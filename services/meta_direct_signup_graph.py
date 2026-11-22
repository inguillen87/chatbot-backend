"""Server-side WhatsApp Embedded Signup. The caller owns the one-use claim and storage."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
import time

from services import meta_credential_operations as ops
from services import meta_whatsapp_credentials as vault
from services.meta_credential_admin_runtime import LOCAL_EXPIRY_SECONDS
from services.meta_whatsapp_runtime import (
    APP_SECRETS_KEY, GRAPH_VERSION_KEY, MetaRuntimeError, _VERSION, _clock_value,
    _object_response, native_graph_request, verify_app_subscription,
    verify_phone_membership, verify_token_authority,
)
from services.tenant_provider_credentials import ProviderCredentialError
from utils.meta_credential_http import private_renewal_logs


@dataclass(frozen=True)
class DirectSignupProof:
    credential: vault.MetaCredential = field(repr=False)
    phone: str = field(repr=False)
    registered: bool


def _require_id(value):
    try:
        return vault.meta_id(value)
    except ProviderCredentialError:
        raise MetaRuntimeError("meta_binding_invalid") from None


class DirectSignupGraph:
    app_id = ops.APP_ID

    def __init__(self, config, *, graph_request=native_graph_request, clock=lambda: int(time.time())):
        authority = ops._authority_config(None, config)
        registry = authority.get(APP_SECRETS_KEY)
        secret = registry.get(self.app_id) if isinstance(registry, Mapping) else None
        version = authority.get(GRAPH_VERSION_KEY, "v25.0")
        if (not isinstance(secret, str) or not 16 <= len(secret) <= 4096
                or not isinstance(version, str) or not _VERSION.fullmatch(version)):
            raise MetaRuntimeError("meta_app_authority_unconfigured")
        self._base = "https://graph.facebook.com/" + version + "/"
        self._secret = secret
        self._request = graph_request
        self._clock = clock

    def _call(self, method, path, *, bearer=None, form=None, **kwargs):
        headers = {}
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            kwargs["data"] = form
        elif method == "POST":
            headers["Content-Type"] = "application/json"
            kwargs["json"] = {}
        with private_renewal_logs():
            try:
                response = self._request(method, self._base + path, headers=headers, **kwargs,
                                         allow_redirects=False, verify=True, timeout=(3, 10))
            except Exception:
                # A transport error may contain the entire request or provider body.
                raise MetaRuntimeError("meta_authority_temporarily_unavailable", transient=True) from None
            payload = _object_response(response)
            if "error" in payload:
                raise MetaRuntimeError("meta_authority_response_invalid", transient=True)
            return payload

    def exchange_and_verify(self, code, waba_id, phone_number_id):
        """Consume a previously claimed code once, verifying its assets before subscribing."""
        _require_id(waba_id)
        _require_id(phone_number_id)
        if not isinstance(code, str) or not re.fullmatch(r"[!-~]{1,4096}", code):
            raise MetaRuntimeError("meta_signup_code_invalid")
        now = _clock_value(self._clock)
        exchange = self._call("POST", "oauth/access_token", form={
            "client_id": self.app_id, "client_secret": self._secret, "code": code,
            "grant_type": "authorization_code",
        })
        token = exchange.get("access_token")
        token_type = exchange.get("token_type")
        if (not isinstance(token, str) or not vault._TOKEN.fullmatch(token)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"):
            raise MetaRuntimeError("meta_token_authority_invalid")
        expires_in = exchange.get("expires_in", 0)
        if type(expires_in) is not int or expires_in < 0:
            raise MetaRuntimeError("meta_token_authority_invalid")
        expiry = now + min(LOCAL_EXPIRY_SECONDS, expires_in or LOCAL_EXPIRY_SECONDS)
        return self.verify_credential(waba_id, phone_number_id, vault.MetaCredential(1, expiry, token))

    def verify_credential(self, waba_id, phone_number_id, credential):
        """Recheck the credential of an in-progress signup without consuming another code."""
        _require_id(waba_id)
        _require_id(phone_number_id)
        now = _clock_value(self._clock)
        if credential.expires_at <= now:
            raise MetaRuntimeError("meta_token_authority_expired")
        token = credential.access_token
        # Introspect via Graph's GET method override in a form POST; the input
        # token never becomes part of the HTTP request target or a proxy URL.
        observed = self._call("POST", "debug_token", bearer=self.app_id + "|" + self._secret,
                              form={"method": "GET", "input_token": token}).get("data")
        expiries = verify_token_authority(observed, self.app_id, waba_id, now, for_renewal=True)
        phones = self._call("GET", waba_id + "/phone_numbers", bearer=token,
                            params={"fields": "id,display_phone_number,status", "limit": "100"})
        row = verify_phone_membership(phones, phone_number_id)
        display = row.get("display_phone_number")
        if not isinstance(display, str) or not re.fullmatch(r"\+?[()0-9 -]{1,63}", display):
            raise MetaRuntimeError("meta_phone_membership_unverified")
        digits = re.sub(r"[() -]", "", display.removeprefix("+"))
        if not re.fullmatch(r"[1-9][0-9]{1,14}", digits):
            raise MetaRuntimeError("meta_phone_membership_unverified")
        expiry = min([credential.expires_at, *expiries])
        if not now <= _clock_value(self._clock) < min(expiry, now + 60):
            raise MetaRuntimeError("meta_token_authority_expired")
        return DirectSignupProof(vault.MetaCredential(credential.revision, expiry, token), "+" + digits,
                                 row.get("status") == "CONNECTED")

    def _subscription_exists(self, path, token):
        listed = self._call("GET", path, bearer=token)
        return verify_app_subscription(listed, self.app_id, allow_absent=True, require_default_callback=True)

    def subscribe_and_verify(self, waba_id, credential):
        """Subscribe using the existing app callback, then read back the exact app ID."""
        _require_id(waba_id)
        if _clock_value(self._clock) >= credential.expires_at:
            raise MetaRuntimeError("meta_token_authority_expired")
        path = waba_id + "/subscribed_apps"
        if not self._subscription_exists(path, credential.access_token):
            if _clock_value(self._clock) >= credential.expires_at:
                raise MetaRuntimeError("meta_token_authority_expired")
            response = self._call("POST", path, bearer=credential.access_token)
            if response.get("success") is not True:
                raise MetaRuntimeError("meta_app_subscription_unverified")
            if not self._subscription_exists(path, credential.access_token):
                raise MetaRuntimeError("meta_app_subscription_unverified")
        if _clock_value(self._clock) >= credential.expires_at:
            raise MetaRuntimeError("meta_token_authority_expired")
