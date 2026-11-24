"""Production Meta authority/transport adapters; no credential installation.

The app-secret registry is private application configuration. This module never
loads dotenv, falls back to TDF/Twilio, or logs provider requests/errors. Tests
must inject HTTP adapters; the native adapter refuses network while testing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import http.client
import json
import os
import re
import ssl
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

from flask import current_app, has_app_context

from services import meta_whatsapp_cloud as cloud
from services.outbox_execution_budget import (
    outbox_io_timeout_seconds, require_outbox_time_remaining,
)


APP_SECRETS_KEY = "META_WHATSAPP_APP_SECRETS"
GRAPH_VERSION_KEY = "META_WHATSAPP_GRAPH_VERSION"
MAX_GRAPH_BYTES = 64 * 1024
_VERSION = re.compile(r"v[1-9][0-9]{0,2}\.0\Z")
_SCOPES = {"whatsapp_business_management", "whatsapp_business_messaging"}


class MetaRuntimeError(cloud.MetaContractError):
    def __init__(self, code: str, *, transient: bool = False):
        self.code = code
        self.transient = transient
        self.whatsapp_outbound_permanent = not transient
        super().__init__(code)


def _clock_value(clock: Callable[[], int]) -> int:
    now = clock()
    if type(now) is not int or now < 0:
        raise MetaRuntimeError("meta_runtime_clock_invalid")
    return now


def native_graph_request(method: str, url: str, **kwargs):
    """One TLS request, no redirect or retry, with bounded response bytes."""
    if not has_app_context():
        raise MetaRuntimeError("meta_graph_app_context_required")
    if (current_app.config.get("TESTING")
            or str(os.getenv("TESTING", "")).lower() in {"1", "true", "yes", "on"}):
        raise MetaRuntimeError("meta_native_network_disabled_in_tests")
    require_outbox_time_remaining()
    parsed = urlsplit(url)
    if (method not in {"GET", "POST"} or parsed.scheme != "https"
            or parsed.hostname != "graph.facebook.com" or parsed.port is not None
            or parsed.username or parsed.password or parsed.fragment
            or not re.fullmatch(r"/v[1-9][0-9]{0,2}\.0/[A-Za-z0-9_/]+", parsed.path)
            or parsed.query or kwargs.get("allow_redirects") is not False
            or kwargs.get("verify") is not True):
        raise MetaRuntimeError("meta_graph_transport_contract_invalid")
    path = parsed.path
    params = kwargs.get("params")
    if params:
        path += "?" + urlencode(params)
    body = None
    if method == "POST":
        if "data" in kwargs:
            if (not parsed.path.endswith(("/oauth/access_token", "/debug_token"))
                    or params or "json" in kwargs
                    or kwargs.get("headers", {}).get("Content-Type") != "application/x-www-form-urlencoded"):
                raise MetaRuntimeError("meta_graph_transport_contract_invalid")
            body = urlencode(kwargs["data"]).encode("ascii")
        else:
            body = json.dumps(kwargs.get("json"), ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_GRAPH_BYTES:
            raise MetaRuntimeError("meta_graph_request_too_large")
    connection = http.client.HTTPSConnection("graph.facebook.com",
        timeout=min(3.0, outbox_io_timeout_seconds(3.0) or 3.0),
        context=ssl.create_default_context())
    connection.set_debuglevel(0)
    deadline = time.monotonic() + min(13.0, outbox_io_timeout_seconds(13.0) or 13.0)
    try:
        connection.connect()
        require_outbox_time_remaining()
        connection.sock.settimeout(outbox_io_timeout_seconds(10.0) or 10.0)
        connection.request(method, path, body=body, headers=kwargs.get("headers", {}))
        require_outbox_time_remaining()
        connection.sock.settimeout(outbox_io_timeout_seconds(10.0) or 10.0)
        response = connection.getresponse()
        chunks, length = [], 0
        while True:
            remaining = deadline - time.monotonic()
            require_outbox_time_remaining()
            if remaining <= 0:
                raise MetaRuntimeError("meta_graph_transport_unavailable", transient=True)
            if connection.sock is not None:
                connection.sock.settimeout(min(10.0, remaining,
                    outbox_io_timeout_seconds(10.0) or 10.0))
            # read1 returns after one available buffered/socket read. read(n)
            # may keep accumulating a slow trickle inside one call, hiding the
            # execution deadline from this loop and its persistence reserve.
            chunk = response.read1(min(8192, MAX_GRAPH_BYTES + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > MAX_GRAPH_BYTES:
                raise MetaRuntimeError("meta_graph_response_too_large", transient=True)
        return SimpleNamespace(status_code=response.status, content=b"".join(chunks))
    except MetaRuntimeError:
        raise
    except Exception:
        # Never chain a transport error that might contain input_token or URLs.
        raise MetaRuntimeError("meta_graph_transport_unavailable", transient=True) from None
    finally:
        connection.close()


def _object_response(response) -> dict:
    status = getattr(response, "status_code", None)
    if type(status) is not int:
        raise MetaRuntimeError("meta_authority_response_invalid", transient=True)
    if status == 429 or status >= 500 or status < 200 or 300 <= status < 400:
        raise MetaRuntimeError("meta_authority_temporarily_unavailable", transient=True)
    if not 200 <= status < 300:
        raise MetaRuntimeError("meta_authority_denied")
    return _object_body(response)


def _object_body(response) -> dict:
    raw = getattr(response, "content", None)
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_GRAPH_BYTES:
            raise ValueError
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise MetaRuntimeError("meta_authority_response_invalid", transient=True) from None


def _report_granular_mismatch(granular, waba_id):
    """Describe why the granular scopes did not match, without identifiers.

    Logging is suppressed on the credential paths, so this writes one JSON line
    to stdout with counts and booleans only: never the token, the target ids
    or any other provider value.
    """
    shape = {}
    for scope in sorted(_SCOPES):
        entries = [x for x in granular if isinstance(x, dict) and x.get("scope") == scope]
        targets = entries[0].get("target_ids") if len(entries) == 1 else None
        shape[scope] = {
            "entries": len(entries),
            "target_ids_present": isinstance(targets, list),
            "target_count": len(targets) if isinstance(targets, list) else None,
            "includes_expected_account": isinstance(targets, list) and waba_id in targets,
        }
    other = sorted({x.get("scope") for x in granular if isinstance(x, dict)
                    and isinstance(x.get("scope"), str) and re.fullmatch(r"[a-z_]{1,64}", x["scope"])}
                   - set(_SCOPES))
    print(json.dumps({"event": "meta_token_granular_scope_mismatch", "scopes": shape,
                      "other_scopes": other}), flush=True)


def verify_token_authority(observed, app_id, waba_id, now, *, for_renewal):
    if for_renewal and (not isinstance(observed, dict)
                        or type(observed.get("is_valid")) is not bool):
        raise MetaRuntimeError("meta_authority_response_invalid", transient=True)
    if (not isinstance(observed, dict) or observed.get("is_valid") is not True
            or observed.get("app_id") != app_id
            or not isinstance(observed.get("scopes"), list)
            or any(not isinstance(scope, str) for scope in observed["scopes"])
            or not _SCOPES.issubset(observed["scopes"])):
        raise MetaRuntimeError("meta_token_authority_invalid")
    provider_expiries = []
    for name in ("expires_at", "data_access_expires_at"):
        if name not in observed:
            if name == "expires_at":
                raise MetaRuntimeError("meta_token_authority_invalid", transient=for_renewal)
            continue
        value = observed[name]
        if type(value) is not int or value < 0:
            raise MetaRuntimeError("meta_token_authority_expired", transient=for_renewal)
        if value and value <= now:
            raise MetaRuntimeError("meta_token_authority_expired")
        if value:
            provider_expiries.append(value)
    granular = observed.get("granular_scopes")
    if for_renewal and granular is None:
        raise MetaRuntimeError("meta_token_granular_scope_mismatch")
    if granular is not None:
        if not isinstance(granular, list):
            raise MetaRuntimeError("meta_token_granular_scope_invalid", transient=for_renewal)
        for scope in _SCOPES:
            entries = [x for x in granular if isinstance(x, dict) and x.get("scope") == scope]
            if (len(entries) != 1 or not isinstance(entries[0].get("target_ids"), list)
                    or any(not isinstance(value, str) for value in entries[0]["target_ids"])
                    or waba_id not in entries[0]["target_ids"]):
                _report_granular_mismatch(granular, waba_id)
                raise MetaRuntimeError("meta_token_granular_scope_mismatch")
    return provider_expiries


def verify_phone_membership(phones, phone_number_id):
    if (not isinstance(phones.get("paging", {}), dict) or phones.get("paging", {}).get("next")
            or not isinstance(phones.get("data"), list)):
        raise MetaRuntimeError("meta_phone_membership_unverified")
    matches = [row for row in phones["data"] if isinstance(row, dict) and row.get("id") == phone_number_id]
    if len(matches) != 1:
        raise MetaRuntimeError("meta_phone_membership_unverified")
    return matches[0]


def verify_app_subscription(apps, app_id, *, allow_absent=False, require_default_callback=False):
    if (not isinstance(apps.get("paging", {}), dict)
            or apps.get("paging", {}).get("next") or not isinstance(apps.get("data"), list)):
        raise MetaRuntimeError("meta_app_subscription_unverified")
    rows = [row for row in apps["data"]
            if isinstance(row, dict) and isinstance(row.get("whatsapp_business_api_data"), dict)]
    ids = [row["whatsapp_business_api_data"].get("id") for row in rows]
    if allow_absent:
        if (len(ids) != len(apps["data"])
                or any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,39}", value)
                       for value in ids)):
            raise MetaRuntimeError("meta_app_subscription_unverified")
        if app_id not in ids:
            return False
    if ids.count(app_id) != 1:
        raise MetaRuntimeError("meta_app_subscription_unverified")
    if require_default_callback and rows[ids.index(app_id)].get("override_callback_uri") not in (None, ""):
        raise MetaRuntimeError("meta_app_subscription_unverified")
    return True


@dataclass(frozen=True)
class MetaOutboundRuntime:
    config: Mapping[str, Any] = field(repr=False)
    get: Callable = field(default=native_graph_request, repr=False)
    post: Callable = field(default=native_graph_request, repr=False)
    clock: Callable[[], int] = field(default=lambda: int(time.time()), repr=False)

    @classmethod
    def from_app(cls):
        return cls(current_app.config)

    def now(self) -> int:
        return _clock_value(self.clock)

    def authority(self, sender: cloud.MetaSenderSnapshot, credential, *, observed_at=None):
        proof, _ = self.observe_authority(sender, credential, observed_at=observed_at)
        return proof

    def observe_authority(self, sender, credential, *, observed_at=None, for_renewal=False,
                          require_default_callback=False):
        """Return the short-lived authority proof and the earliest finite provider expiry."""
        now = self.now() if observed_at is None else observed_at
        registry = self.config.get(APP_SECRETS_KEY)
        secret = registry.get(sender.app_id) if isinstance(registry, Mapping) else None
        version = self.config.get(GRAPH_VERSION_KEY, "v25.0")
        if (not isinstance(secret, str) or not 16 <= len(secret) <= 4096
                or not isinstance(version, str) or not _VERSION.fullmatch(version)):
            raise MetaRuntimeError("meta_app_authority_unconfigured")
        base = "https://graph.facebook.com/" + version + "/"
        def get(path, token, params=None):
            try:
                require_outbox_time_remaining()
                timeout = outbox_io_timeout_seconds(10.0) or 10.0
                response = self.get("GET", base + path,
                    headers={"Authorization": "Bearer " + token}, params=params,
                    timeout=(min(3.0, timeout), timeout), allow_redirects=False, verify=True)
            except MetaRuntimeError:
                raise
            except Exception:
                raise MetaRuntimeError("meta_authority_temporarily_unavailable", transient=True) from None
            payload = _object_response(response)
            if for_renewal and path != "debug_token":
                paging = payload.get("paging", {})
                if (not isinstance(payload.get("data"), list) or not isinstance(paging, dict)
                        or paging.get("next")):
                    # An incomplete listing cannot establish a revoked binding.
                    raise MetaRuntimeError("meta_authority_response_invalid", transient=True)
            return payload
        observed = get("debug_token", sender.app_id + "|" + secret,
                       {"input_token": credential.access_token}).get("data")
        provider_expiries = verify_token_authority(observed, sender.app_id, sender.waba_id, now,
                                                 for_renewal=for_renewal)
        phones = get(sender.waba_id + "/phone_numbers", credential.access_token,
                     {"fields": "id", "limit": "100"})
        verify_phone_membership(phones, sender.phone_number_id)
        apps = get(sender.waba_id + "/subscribed_apps", credential.access_token)
        verify_app_subscription(apps, sender.app_id, require_default_callback=require_default_callback)
        final_now = self.now()
        valid_until = min(credential.expires_at, now + 60, *provider_expiries)
        if not now <= final_now < valid_until:
            # If the proof aged out during I/O, a new observation is needed.
            raise MetaRuntimeError("meta_token_authority_expired", transient=for_renewal)
        proof = cloud.VerifiedMetaAuthority(sender, credential.revision, version,
            now, valid_until, True, True)
        return proof, min(provider_expiries, default=None)

    def load_binding(self, identity, *, session=None):
        failures = []
        observed_at = self.now()
        def verify(sender, credential):
            try:
                return self.authority(sender, credential, observed_at=observed_at)
            except MetaRuntimeError as exc:
                failures.append(exc)
                raise
        try:
            return cloud.resolve_sender(phone_number_id=identity.phone_number_id,
                app_id=identity.app_id, waba_id=identity.waba_id,
                environment="production", now=observed_at, app_config=self.config,
                authority_verifier=verify, expected_tenant_id=identity.tenant_id,
                session=session)
        except cloud.MetaContractError:
            # resolve_sender deliberately redacts callbacks; retain only our
            # finite typed failure, never arbitrary callback/provider errors.
            if failures:
                raise failures[0] from None
            raise

    def recheck_local_binding(self, expected, *, session=None):
        binding = cloud.resolve_sender(phone_number_id=expected.sender.phone_number_id,
            app_id=expected.sender.app_id, waba_id=expected.sender.waba_id,
            environment="production", now=self.now(), app_config=self.config,
            expected_tenant_id=expected.sender.tenant_id, session=session,
            authority_verifier=lambda _sender, _credential: expected.authority)
        if (binding.sender != expected.sender
                or binding.credential.revision != expected.credential.revision
                or not hmac.compare_digest(binding.credential.access_token,
                                           expected.credential.access_token)):
            raise MetaRuntimeError("meta_outbound_authority_retired")
        return binding
