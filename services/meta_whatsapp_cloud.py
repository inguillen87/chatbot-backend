"""Unwired Cloud API foundation. No registration, activation or default transport.

Local IDs/statuses never grant authority. A trusted future adapter must supply a
fresh Meta observation; actor/consent/window/template policy remains mandatory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import re
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from services.meta_whatsapp_credentials import MetaCredential, meta_id, open_token
from services.tenant_provider_credentials import ProviderCredentialError, _positive

_VERSION = re.compile(r"^v[1-9][0-9]{0,2}\.0$")
_RECIPIENT = re.compile(r"^[1-9][0-9]{6,14}$")
_WAMID = re.compile(r"^wamid\.[A-Za-z0-9_+=/-]{1,170}$")
_NAME = re.compile(r"^[a-z0-9_]{1,255}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?$")
MAX_RESPONSE_BYTES = 64 * 1024


class MetaContractError(RuntimeError):
    """Fixed reason codes only. Never include payloads, tokens or provider errors."""


@dataclass(frozen=True)
class MetaSenderSnapshot:
    tenant_id: int
    connection_id: int
    sender_id: int
    app_id: str
    waba_id: str
    phone_number_id: str
    environment: str


@dataclass(frozen=True)
class VerifiedMetaAuthority:
    """Adapter result, NOT a claim that this foundation observed a real account.

    There is no default adapter or persisted marker writer in this patch. The
    eventual adapter must verify token app/scopes, WABA membership, registered
    phone and webhook subscription against Meta, bound to credential revision.
    """
    sender: MetaSenderSnapshot
    credential_revision: int
    graph_version: str
    observed_at: int
    valid_until: int
    messaging_permitted: bool
    webhook_subscribed: bool


@dataclass(frozen=True)
class VerifiedMetaBinding:
    sender: MetaSenderSnapshot
    authority: VerifiedMetaAuthority
    credential: MetaCredential = field(repr=False)


def resolve_sender(*, phone_number_id: str, app_id: str, waba_id: str,
                   environment: str, now: int, app_config: Mapping,
                   authority_verifier: Callable | None = None,
                   expected_tenant_id: int | None = None, session=None) -> VerifiedMetaBinding:
    """Global exact sender uniqueness first, persisted column snapshots, no flush.

    expected_tenant_id is an additional constraint, never an authority source.
    Duplicate/dangling/foreign-provider rows fail closed rather than picking one.
    """
    from sqlalchemy import select
    from models import ProviderConnection as C, ProviderSender as S, TenantProfile as T, db
    from services.provider_platform import is_sender_ready_status

    phone_number_id, app_id, waba_id = map(meta_id, (phone_number_id, app_id, waba_id))
    if (environment not in {"production", "sandbox"} or type(now) is not int or now < 0
            or authority_verifier is None or not callable(authority_verifier)):
        raise MetaContractError("meta_verified_authority_required")
    if expected_tenant_id is not None:
        _positive(expected_tenant_id)
    session = session if session is not None else db.session
    rows = session.execute(select(
        S.id.label("sender_id"), S.tenant_id.label("sender_tenant_id"), S.waba_id,
        S.provider_connection_id, S.phone_number_id, S.status.label("sender_status"),
        C.id.label("id"), C.tenant_id, C.provider, C.channel, C.environment,
        C.external_app_id, C.external_account_id, C.credentials_ref, C.config,
        C.status.label("connection_status"),
        T.is_active,
    ).select_from(S).outerjoin(C, C.id == S.provider_connection_id)
        .outerjoin(T, T.id == S.tenant_id)
        .where(S.channel == "whatsapp", S.phone_number_id == phone_number_id)
        .limit(2).execution_options(autoflush=False)).all()
    if len(rows) != 1:
        raise MetaContractError("meta_sender_unknown_or_conflicting")
    row = rows[0]._mapping
    if (row["sender_tenant_id"] != row["tenant_id"] or row["is_active"] is not True
            or row["external_app_id"] != app_id or row["external_account_id"] != waba_id
            or row["waba_id"] != waba_id or row["environment"] != environment
            or (expected_tenant_id is not None and row["tenant_id"] != expected_tenant_id)):
        raise MetaContractError("meta_sender_binding_mismatch")
    # Persisted local suspension/disconnection vetoes even fresh remote authority.
    # Being locally ready remains only a prerequisite, never authorization.
    if (not is_sender_ready_status(row["sender_status"])
            or not is_sender_ready_status(row["connection_status"])):
        raise MetaContractError("meta_sender_locally_unavailable")
    connection = SimpleNamespace(**{key: row[key] for key in (
        "id", "tenant_id", "provider", "channel", "environment", "external_app_id",
        "external_account_id", "credentials_ref", "config")})
    credential = open_token(connection=connection, tenant_id=row["tenant_id"],
                            now=now, app_config=app_config)
    sender = MetaSenderSnapshot(_positive(row["tenant_id"]), _positive(row["id"]),
                                _positive(row["sender_id"]), app_id, waba_id,
                                phone_number_id, environment)
    # The callback is trusted server code, never supplied by an HTTP actor.
    try:
        authority = authority_verifier(sender, credential)
    except Exception:
        raise MetaContractError("meta_authority_verification_failed") from None
    if (not isinstance(authority, VerifiedMetaAuthority) or authority.sender != sender
            or type(authority.credential_revision) is not int
            or authority.credential_revision != credential.revision
            or not isinstance(authority.graph_version, str)
            or not _VERSION.fullmatch(authority.graph_version)
            or type(authority.observed_at) is not int or type(authority.valid_until) is not int
            or not 0 <= authority.observed_at <= now < authority.valid_until
            or authority.valid_until > credential.expires_at):
        raise MetaContractError("meta_authority_binding_invalid")
    return VerifiedMetaBinding(sender, authority, credential)


def text_payload(*, recipient: str, body: str) -> dict:
    if (not isinstance(recipient, str) or not _RECIPIENT.fullmatch(recipient)
            or not isinstance(body, str) or not body.strip() or len(body) > 4096):
        raise MetaContractError("meta_message_payload_invalid")
    return dict(messaging_product="whatsapp", recipient_type="individual", to=recipient,
                type="text", text={"preview_url": False, "body": body})


def template_payload(*, recipient: str, name: str, language: str,
                     body_parameters: tuple[str, ...] = ()) -> dict:
    """Text-body templates only; approval/consent are separately required to send."""
    if (not isinstance(recipient, str) or not _RECIPIENT.fullmatch(recipient)
            or not isinstance(name, str) or not _NAME.fullmatch(name)
            or not isinstance(language, str) or not _LANGUAGE.fullmatch(language)
            or not isinstance(body_parameters, tuple) or len(body_parameters) > 100
            or any(not isinstance(value, str) or not value or len(value) > 1024
                   for value in body_parameters)):
        raise MetaContractError("meta_template_payload_invalid")
    template = {"name": name, "language": {"code": language}}
    if body_parameters:
        template["components"] = [{"type": "body", "parameters": [
            {"type": "text", "text": value} for value in body_parameters]}]
    return dict(messaging_product="whatsapp", recipient_type="individual", to=recipient,
                type="template", template=template)


@dataclass(frozen=True)
class MetaSendResult:
    state: str
    http_status: int | None = None
    message_id: str | None = field(default=None, repr=False)
    error_code: int | None = None
    retry_allowed: bool = False


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def send_once(*, binding_loader: Callable[[], VerifiedMetaBinding], payload: dict,
              now: int, policy_check: Callable | None, post: Callable | None) -> MetaSendResult:
    """One injected HTTPS POST. No redirects/retries; uncertainty needs reconcile.

    The adapter MUST have TLS validation enabled and automatic retries disabled.
    No requests Session/SDK/environment transport is created by this module.
    A fresh loader and policy check are required for every call, including replay.
    """
    if post is None or not callable(post) or policy_check is None or not callable(policy_check):
        raise MetaContractError("meta_send_adapters_required")
    if not isinstance(payload, dict):
        raise MetaContractError("meta_message_payload_invalid")
    # Reconstruct an allowlisted detached body; reject hidden/extra keys.
    try:
        if payload.get("type") == "text":
            safe = text_payload(recipient=payload["to"], body=payload["text"]["body"])
        elif payload.get("type") == "template":
            tpl = payload["template"]
            parts = tpl.get("components", [])
            params = tuple(p["text"] for p in parts[0]["parameters"]) if parts else ()
            safe = template_payload(recipient=payload["to"], name=tpl["name"],
                                    language=tpl["language"]["code"], body_parameters=params)
        else:
            raise ValueError
        if payload != safe:
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise MetaContractError("meta_message_payload_invalid") from None
    try:
        binding = binding_loader()
    except (MetaContractError, ProviderCredentialError):
        raise
    except Exception:
        raise MetaContractError("meta_send_binding_unavailable") from None
    if (not isinstance(binding, VerifiedMetaBinding) or type(now) is not int or now < 0
            or binding.authority.sender != binding.sender
            or binding.authority.credential_revision != binding.credential.revision
            or not binding.authority.observed_at <= now < binding.authority.valid_until
            or now >= binding.credential.expires_at
            or binding.authority.messaging_permitted is not True
            or binding.authority.webhook_subscribed is not True
            or not isinstance(binding.authority.graph_version, str)
            or not _VERSION.fullmatch(binding.authority.graph_version)):
        raise MetaContractError("meta_send_authority_unavailable")
    # Actor authorization + consent + service window or approved tenant template.
    try:
        permitted = policy_check(binding.sender, copy.deepcopy(safe))
    except Exception:
        raise MetaContractError("meta_outbound_policy_unavailable") from None
    if permitted is not True:
        raise MetaContractError("meta_outbound_policy_denied")
    url = (f"https://graph.facebook.com/{binding.authority.graph_version}/"
           f"{meta_id(binding.sender.phone_number_id)}/messages")
    try:
        response = post(url, json=safe, headers={
            "Authorization": "Bearer " + binding.credential.access_token,
            "Content-Type": "application/json"}, timeout=(3.0, 10.0),
            allow_redirects=False, verify=True)
    except Exception:
        return MetaSendResult("uncertain")
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        return MetaSendResult("uncertain")
    # A redirected, server-failed or malformed success may have accepted the POST.
    if status < 200 or 300 <= status < 400 or status >= 500:
        return MetaSendResult("uncertain", status)
    try:
        raw = response.content
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RESPONSE_BYTES:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        return MetaSendResult("uncertain", status)
    if 200 <= status < 300:
        messages = data.get("messages") if isinstance(data, dict) else None
        if (not isinstance(messages, list) or len(messages) != 1
                or not isinstance(messages[0], dict)
                or not isinstance(messages[0].get("id"), str)
                or not _WAMID.fullmatch(messages[0]["id"])
                or data.get("messaging_product") != "whatsapp"):
            return MetaSendResult("uncertain", status)
        return MetaSendResult("accepted", status, messages[0]["id"])
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return MetaSendResult("rejected", status, error_code=code if type(code) is int and 0 <= code <= 9999999 else None)
