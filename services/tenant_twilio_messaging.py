"""Tenant-bound Twilio sender resolution and outbound message delivery.

This adapter is deliberately independent from the WhatsApp webhook route.  A
durable domain effect first binds itself to the current ``ProviderSender`` and
``ProviderConnection`` through an opaque digest.  At dispatch time the digest
is recomputed and all tenant/account/from-address checks happen before the
outbox crosses its provider-I/O boundary.

The adapter never falls back to deployment-wide ``TWILIO_*`` sender numbers.
Parent credentials may only be used when an explicit tenant-owned connection
points at that exact parent account and an explicit ready sender is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import math
import re
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID

from flask import current_app
from sqlalchemy.orm import joinedload
from twilio.rest import Client

from models import (
    MessageTemplateRegistry,
    Notification,
    NotificationAttempt,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    db,
)
from services.llm_provider_network_policy import (
    ProviderNetworkDisabledError,
    require_provider_network,
)
from services.outbox_execution_budget import outbox_twilio_http_client
from services.provider_platform import is_sender_ready_status
from services.message_templates import whatsapp_template_lifecycle
from services.twilio_tech_provider import resolve_twilio_runtime_credentials


TwilioChannel = Literal["sms", "whatsapp"]

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MESSAGE_SERVICE_RE = re.compile(r"^MG[A-Za-z0-9]{8,64}$")
_TWILIO_CONTENT_SID_RE = re.compile(r"^HX[0-9a-fA-F]{32}$")
_CONTENT_VARIABLE_KEY_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_STATUS_CALLBACK_ATTEMPT_PARAM = "notification_attempt_id"
_MAX_CALLBACK_URL_LENGTH = 2048
_MAX_CONTENT_VARIABLES = 100
_MAX_CONTENT_VARIABLE_LENGTH = 1000
_MAX_CONTENT_VARIABLES_JSON_LENGTH = 32768


class TenantTwilioScopeError(RuntimeError):
    """A permanent tenant/sender binding violation detected before I/O."""

    def __init__(self, code: str):
        self.code = str(code or "tenant_twilio_scope_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class TenantTwilioSenderSnapshot:
    tenant_id: int
    channel: TwilioChannel
    binding: str
    sender: ProviderSender | None = field(default=None, repr=False)
    reason_code: str | None = None


@dataclass(frozen=True)
class PreparedTenantTwilioMessage:
    """Immutable provider call assembled only after complete preflight."""

    account_sid: str
    auth_token: str = field(repr=False)
    provider_sender_id: int
    params: Mapping[str, Any]


@dataclass(frozen=True)
class TenantTwilioMessagePreflight:
    prepared: PreparedTenantTwilioMessage | None = None
    reason_code: str | None = None


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _connection_document(connection: ProviderConnection | None) -> dict[str, Any]:
    if connection is None:
        return {"id": None}
    return {
        "id": _positive_int(getattr(connection, "id", None)),
        "tenant_id": _positive_int(getattr(connection, "tenant_id", None)),
        "provider": _clean(getattr(connection, "provider", None)).lower(),
        "channel": _clean(getattr(connection, "channel", None)).lower(),
        "external_account_id": _clean(
            getattr(connection, "external_account_id", None)
        ),
        "credentials_ref": _clean(getattr(connection, "credentials_ref", None)),
    }


def _sender_document(sender: ProviderSender) -> dict[str, Any]:
    return {
        "id": _positive_int(getattr(sender, "id", None)),
        "tenant_id": _positive_int(getattr(sender, "tenant_id", None)),
        "connection": _connection_document(
            getattr(sender, "provider_connection", None)
        ),
        "channel": _clean(getattr(sender, "channel", None)).lower(),
        "sender_id": _clean(getattr(sender, "sender_id", None)),
        "phone_number": _clean(getattr(sender, "phone_number", None)),
        "messaging_service_sid": _clean(
            getattr(sender, "messaging_service_sid", None)
        ),
        # The callback receives provider delivery metadata. Bind it together
        # with the sender so it cannot be redirected after durable staging.
        "status_callback_url": _clean(
            getattr(sender, "status_callback_url", None)
        ),
    }


def _binding_digest(
    *,
    tenant_id: int,
    channel: TwilioChannel,
    state: str,
    senders: Sequence[ProviderSender],
) -> str:
    document = {
        "contract": "tenant-twilio-sender-binding.v1",
        "tenant_id": int(tenant_id),
        "channel": channel,
        "state": state,
        "senders": sorted(
            (_sender_document(sender) for sender in senders),
            key=lambda item: int(item.get("id") or 0),
        ),
    }
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_tenant_twilio_sender_snapshot(
    *,
    tenant_id: int,
    channel: TwilioChannel,
    session=None,
) -> TenantTwilioSenderSnapshot:
    """Resolve one unambiguous ready Twilio sender owned by ``tenant_id``.

    A snapshot is returned even for missing/invalid configuration so the
    staged effect is bound to that fail-closed state.  Adding or replacing a
    sender before dispatch therefore produces a binding mismatch instead of a
    send from newly discovered branding.
    """

    normalized_tenant_id = _positive_int(tenant_id)
    if normalized_tenant_id is None:
        raise TenantTwilioScopeError("tenant_twilio_tenant_invalid")
    if channel not in {"sms", "whatsapp"}:
        raise TenantTwilioScopeError("tenant_twilio_channel_invalid")

    effect_session = session or db.session
    candidates = (
        effect_session.query(ProviderSender)
        .options(joinedload(ProviderSender.provider_connection))
        .filter(
            ProviderSender.tenant_id == normalized_tenant_id,
            ProviderSender.channel == channel,
        )
        .order_by(ProviderSender.id.asc())
        .all()
    )
    ready = [
        sender
        for sender in candidates
        if is_sender_ready_status(getattr(sender, "status", None))
    ]

    if not candidates:
        reason = f"{channel}_tenant_sender_missing"
        return TenantTwilioSenderSnapshot(
            tenant_id=normalized_tenant_id,
            channel=channel,
            binding=_binding_digest(
                tenant_id=normalized_tenant_id,
                channel=channel,
                state=reason,
                senders=(),
            ),
            reason_code=reason,
        )
    if not ready:
        reason = f"{channel}_tenant_sender_not_ready"
        return TenantTwilioSenderSnapshot(
            tenant_id=normalized_tenant_id,
            channel=channel,
            binding=_binding_digest(
                tenant_id=normalized_tenant_id,
                channel=channel,
                state=reason,
                senders=candidates,
            ),
            reason_code=reason,
        )
    if len(ready) != 1:
        reason = f"{channel}_tenant_sender_ambiguous"
        return TenantTwilioSenderSnapshot(
            tenant_id=normalized_tenant_id,
            channel=channel,
            binding=_binding_digest(
                tenant_id=normalized_tenant_id,
                channel=channel,
                state=reason,
                senders=ready,
            ),
            reason_code=reason,
        )

    sender = ready[0]
    connection = getattr(sender, "provider_connection", None)
    reason: str | None = None
    if connection is None:
        reason = f"{channel}_tenant_connection_missing"
    elif _positive_int(getattr(connection, "tenant_id", None)) != normalized_tenant_id:
        reason = f"{channel}_tenant_connection_mismatch"
    elif _clean(getattr(connection, "provider", None)).lower() != "twilio":
        reason = f"{channel}_tenant_provider_mismatch"
    elif _clean(getattr(connection, "channel", None)).lower() != channel:
        reason = f"{channel}_tenant_connection_channel_mismatch"
    elif not is_sender_ready_status(getattr(connection, "status", None)):
        reason = f"{channel}_tenant_connection_not_ready"
    elif not _clean(getattr(connection, "external_account_id", None)):
        # Requiring an account identity prevents a tenant canary from silently
        # inheriting whatever deployment-wide Twilio account happens to exist.
        reason = f"{channel}_tenant_connection_account_missing"

    state = reason or "ready"
    return TenantTwilioSenderSnapshot(
        tenant_id=normalized_tenant_id,
        channel=channel,
        binding=_binding_digest(
            tenant_id=normalized_tenant_id,
            channel=channel,
            state=state,
            senders=(sender,),
        ),
        sender=sender if reason is None else None,
        reason_code=reason,
    )


def build_tenant_twilio_sender_binding(
    *,
    tenant_id: int,
    channel: TwilioChannel,
    session=None,
) -> str:
    return resolve_tenant_twilio_sender_snapshot(
        tenant_id=tenant_id,
        channel=channel,
        session=session,
    ).binding


def _normalized_e164(value: Any) -> str | None:
    rendered = _clean(value)
    if rendered.lower().startswith("whatsapp:"):
        rendered = rendered.split(":", 1)[1].strip()
    return rendered if _E164_RE.fullmatch(rendered) else None


def _valid_callback_url(value: Any) -> str | None:
    rendered = _clean(value)
    if (
        not rendered
        or len(rendered) > _MAX_CALLBACK_URL_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in rendered)
    ):
        return None
    try:
        parsed = urlparse(rendered)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.fragment
    ):
        return None
    if parsed.username or parsed.password:
        return None
    return rendered


def _validated_media_urls(values: Sequence[Any]) -> tuple[str, ...] | None:
    normalized: list[str] = []
    for value in values:
        rendered = _clean(value)
        parsed = urlparse(rendered)
        if (
            not rendered
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return None
        normalized.append(rendered)
    return tuple(normalized)


def _serialized_content_variables(
    values: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Return Twilio's JSON string without accepting transport-shaped input."""

    if values is None:
        return None, None
    if not isinstance(values, Mapping) or len(values) > _MAX_CONTENT_VARIABLES:
        return None, "whatsapp_template_variables_invalid"

    normalized_by_index: dict[int, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _CONTENT_VARIABLE_KEY_RE.fullmatch(key):
            return None, "whatsapp_template_variables_invalid"
        index = int(key)
        if index in normalized_by_index:
            return None, "whatsapp_template_variables_invalid"

        if isinstance(value, str):
            rendered = value
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, float) and math.isfinite(value):
            rendered = str(value)
        else:
            return None, "whatsapp_template_variables_invalid"

        # Twilio rejects blank/control-character values and long whitespace
        # runs. Reject them here rather than modifying customer-visible text.
        if (
            not rendered
            or rendered != rendered.strip()
            or len(rendered) > _MAX_CONTENT_VARIABLE_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in rendered)
            or "     " in rendered
        ):
            return None, "whatsapp_template_variables_invalid"
        normalized_by_index[index] = rendered

    indexes = sorted(normalized_by_index)
    if indexes != list(range(1, len(indexes) + 1)):
        return None, "whatsapp_template_variables_invalid"

    normalized = {
        str(index): normalized_by_index[index]
        for index in indexes
    }
    if not normalized:
        return None, None
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) > _MAX_CONTENT_VARIABLES_JSON_LENGTH:
        return None, "whatsapp_template_variables_invalid"
    return serialized, None


def _tenant_template_content_sid(
    *,
    tenant_id: int,
    template_registry_id: Any,
    session,
) -> tuple[str | None, str | None]:
    registry_id = _positive_int(template_registry_id)
    if registry_id is None:
        return None, "whatsapp_template_registry_mismatch"

    registry = (
        session.query(MessageTemplateRegistry)
        .filter(
            MessageTemplateRegistry.id == registry_id,
            MessageTemplateRegistry.tenant_id == int(tenant_id),
            MessageTemplateRegistry.provider == "twilio",
            MessageTemplateRegistry.channel == "whatsapp",
        )
        .one_or_none()
    )
    if registry is None:
        return None, "whatsapp_template_registry_mismatch"

    content_sid = _clean(getattr(registry, "content_sid", None))
    if not _TWILIO_CONTENT_SID_RE.fullmatch(content_sid):
        return None, "whatsapp_template_registry_mismatch"

    lifecycle = whatsapp_template_lifecycle(
        getattr(registry, "status", None),
        source="message_template_registry",
        provider_reference=content_sid,
        observed_at=getattr(registry, "last_sync_at", None),
    )
    if not lifecycle.get("production_send_allowed"):
        return None, "whatsapp_template_not_approved"
    return content_sid, None


def _canonical_notification_attempt_id(value: Any) -> str | None:
    rendered = _clean(value)
    if not rendered:
        return None
    try:
        canonical = str(UUID(rendered))
    except (TypeError, ValueError, AttributeError):
        return None
    return canonical if rendered.lower() == canonical else None


def _status_callback_for_message(
    *,
    tenant_id: int,
    channel: TwilioChannel,
    sender: ProviderSender,
    notification_attempt_id: Any,
    session,
) -> tuple[str | None, str | None]:
    """Derive a per-attempt callback only from the bound sender base URL."""

    raw_callback = _clean(getattr(sender, "status_callback_url", None))
    callback = _valid_callback_url(raw_callback)
    if raw_callback and callback is None:
        return None, f"{channel}_status_callback_invalid"

    canonical_attempt_id: str | None = None
    if notification_attempt_id is not None:
        canonical_attempt_id = _canonical_notification_attempt_id(
            notification_attempt_id
        )
        if canonical_attempt_id is None:
            return None, "notification_attempt_scope_mismatch"
        if callback is None:
            return None, f"{channel}_status_callback_missing"

        attempt = (
            session.query(NotificationAttempt)
            .filter(
                NotificationAttempt.id == canonical_attempt_id,
                NotificationAttempt.tenant_id == int(tenant_id),
                NotificationAttempt.status == NotificationAttempt.STATUS_SENDING,
            )
            .one_or_none()
        )
        notification = (
            session.query(Notification)
            .filter(
                Notification.id == getattr(attempt, "notification_id", None),
                Notification.tenant_id == int(tenant_id),
                Notification.channel == channel,
                Notification.status == Notification.STATUS_SENDING,
                Notification.provider_sender_id == int(sender.id),
                Notification.provider_connection_id
                == int(sender.provider_connection_id),
            )
            .one_or_none()
            if attempt is not None
            else None
        )
        if (
            attempt is None
            or notification is None
            or int(attempt.attempt_number) != int(notification.attempt_count)
        ):
            return None, "notification_attempt_scope_mismatch"

    if callback is None:
        return None, None

    parsed = urlparse(callback)
    try:
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=50,
        )
    except ValueError:
        return None, f"{channel}_status_callback_invalid"

    reserved_present = any(
        key == _STATUS_CALLBACK_ATTEMPT_PARAM for key, _value in query_items
    )
    if canonical_attempt_id is None:
        if reserved_present:
            return None, f"{channel}_status_callback_invalid"
        return callback, None

    query_items = [
        (key, value)
        for key, value in query_items
        if key != _STATUS_CALLBACK_ATTEMPT_PARAM
    ]
    query_items.append((_STATUS_CALLBACK_ATTEMPT_PARAM, canonical_attempt_id))
    derived = urlunparse(parsed._replace(query=urlencode(query_items)))
    validated = _valid_callback_url(derived)
    if validated is None:
        return None, f"{channel}_status_callback_invalid"
    return validated, None


def prepare_bound_tenant_twilio_message(
    *,
    tenant_id: int,
    channel: TwilioChannel,
    expected_sender_binding: str,
    recipient: str,
    body: str | None = None,
    media_urls: Sequence[str] = (),
    template_registry_id: int | None = None,
    content_variables: Mapping[str, Any] | None = None,
    notification_attempt_id: str | None = None,
    session=None,
) -> TenantTwilioMessagePreflight:
    """Validate tenant scope and return an immutable Twilio provider call.

    ``template_registry_id`` is the only accepted template selector. The
    provider ``ContentSid`` is loaded from a fresh approved tenant-owned row;
    callers cannot override it. Likewise, ``notification_attempt_id`` only
    augments the callback base URL already bound into the sender digest.
    """

    if not _HEX_DIGEST_RE.fullmatch(_clean(expected_sender_binding).lower()):
        raise TenantTwilioScopeError("tenant_twilio_sender_binding_invalid")
    effect_session = session or db.session
    snapshot = resolve_tenant_twilio_sender_snapshot(
        tenant_id=tenant_id,
        channel=channel,
        session=effect_session,
    )
    if not hmac.compare_digest(
        snapshot.binding,
        _clean(expected_sender_binding).lower(),
    ):
        raise TenantTwilioScopeError("tenant_twilio_sender_binding_mismatch")
    if snapshot.reason_code:
        return TenantTwilioMessagePreflight(reason_code=snapshot.reason_code)
    sender = snapshot.sender
    if sender is None:  # Defensive: the resolver contract should make this impossible.
        raise TenantTwilioScopeError("tenant_twilio_sender_resolution_invalid")

    tenant = effect_session.get(TenantProfile, int(tenant_id))
    if tenant is None or int(getattr(tenant, "id", 0) or 0) != int(tenant_id):
        raise TenantTwilioScopeError("tenant_twilio_tenant_missing")
    connection = getattr(sender, "provider_connection", None)
    if connection is None:
        raise TenantTwilioScopeError("tenant_twilio_connection_missing")

    credentials = resolve_twilio_runtime_credentials(
        tenant=tenant,
        provider_connection=connection,
        app_config=current_app.config,
    )
    if credentials.scope == "conflict":
        raise TenantTwilioScopeError("tenant_twilio_account_scope_mismatch")
    if not credentials.ready:
        return TenantTwilioMessagePreflight(
            reason_code=f"{channel}_tenant_credentials_missing"
        )
    connection_account_sid = _clean(
        getattr(connection, "external_account_id", None)
    )
    if not connection_account_sid or not hmac.compare_digest(
        connection_account_sid,
        _clean(credentials.account_sid),
    ):
        raise TenantTwilioScopeError("tenant_twilio_account_scope_mismatch")

    normalized_recipient = _normalized_e164(recipient)
    if not normalized_recipient:
        return TenantTwilioMessagePreflight(
            reason_code="requester_phone_invalid"
        )

    normalized_media = _validated_media_urls(tuple(media_urls or ()))
    if normalized_media is None:
        return TenantTwilioMessagePreflight(
            reason_code="whatsapp_media_url_invalid"
        )
    if channel == "sms" and normalized_media:
        return TenantTwilioMessagePreflight(reason_code="sms_media_not_supported")
    # A ticket comment owns at most one attachment.  Refuse an unexpected fan
    # out rather than changing one domain effect into several provider sends.
    if len(normalized_media) > 1:
        return TenantTwilioMessagePreflight(
            reason_code="whatsapp_media_count_invalid"
        )

    normalized_body = _clean(body)
    content_sid: str | None = None
    serialized_variables: str | None = None
    if template_registry_id is not None:
        if channel != "whatsapp":
            return TenantTwilioMessagePreflight(
                reason_code="sms_template_not_supported"
            )
        if normalized_body or normalized_media:
            return TenantTwilioMessagePreflight(
                reason_code="whatsapp_template_payload_conflict"
            )
        content_sid, template_error = _tenant_template_content_sid(
            tenant_id=int(tenant_id),
            template_registry_id=template_registry_id,
            session=effect_session,
        )
        if template_error:
            return TenantTwilioMessagePreflight(reason_code=template_error)
        serialized_variables, variables_error = _serialized_content_variables(
            content_variables
        )
        if variables_error:
            return TenantTwilioMessagePreflight(reason_code=variables_error)
    else:
        if content_variables is not None:
            return TenantTwilioMessagePreflight(
                reason_code="whatsapp_template_registry_required"
            )
        if not normalized_body:
            return TenantTwilioMessagePreflight(
                reason_code=f"{channel}_message_empty"
            )
        if len(normalized_body) > 1600:
            return TenantTwilioMessagePreflight(
                reason_code=f"{channel}_message_too_long"
            )

    messaging_service_sid = _clean(
        getattr(sender, "messaging_service_sid", None)
    )
    sender_address = _normalized_e164(
        getattr(sender, "sender_id", None)
    ) or _normalized_e164(getattr(sender, "phone_number", None))
    params: dict[str, Any] = {
        "to": (
            f"whatsapp:{normalized_recipient}"
            if channel == "whatsapp"
            else normalized_recipient
        ),
    }
    if content_sid:
        params["content_sid"] = content_sid
        if serialized_variables:
            params["content_variables"] = serialized_variables
    else:
        params["body"] = normalized_body
    if messaging_service_sid:
        if not _MESSAGE_SERVICE_RE.fullmatch(messaging_service_sid):
            return TenantTwilioMessagePreflight(
                reason_code=f"{channel}_messaging_service_invalid"
            )
        params["messaging_service_sid"] = messaging_service_sid
    elif sender_address:
        params["from_"] = (
            f"whatsapp:{sender_address}"
            if channel == "whatsapp"
            else sender_address
        )
    else:
        return TenantTwilioMessagePreflight(
            reason_code=f"{channel}_tenant_sender_address_missing"
        )

    if normalized_media:
        params["media_url"] = list(normalized_media)
    callback, callback_error = _status_callback_for_message(
        tenant_id=int(tenant_id),
        channel=channel,
        sender=sender,
        notification_attempt_id=notification_attempt_id,
        session=effect_session,
    )
    if callback_error:
        return TenantTwilioMessagePreflight(reason_code=callback_error)
    if callback:
        params["status_callback"] = callback

    return TenantTwilioMessagePreflight(
        prepared=PreparedTenantTwilioMessage(
            account_sid=_clean(credentials.account_sid),
            auth_token=_clean(credentials.auth_token),
            provider_sender_id=int(sender.id),
            params=params,
        )
    )


def send_prepared_tenant_twilio_message(
    prepared: PreparedTenantTwilioMessage,
    *,
    on_provider_call_start: Callable[[], None] | None = None,
) -> str | None:
    """Perform one provider call and return its acknowledgement SID.

    ``on_provider_call_start`` runs after all local setup and immediately
    before ``messages.create``. A failure before the hook is known pre-I/O; an
    exception after the hook may be an ambiguous provider outcome and must not
    be retried as though no request was sent.
    """

    if not isinstance(prepared, PreparedTenantTwilioMessage):
        raise TypeError("tenant_twilio_prepared_message_invalid")
    if on_provider_call_start is not None and not callable(on_provider_call_start):
        raise TypeError("tenant_twilio_provider_call_hook_invalid")
    try:
        require_provider_network("twilio")
    except ProviderNetworkDisabledError:
        logger.info(
            "Tenant message blocked provider=twilio reason=test_network_disabled"
        )
        raise
    bounded_http_client = outbox_twilio_http_client()
    client_kwargs = (
        {"http_client": bounded_http_client}
        if bounded_http_client is not None
        else {}
    )
    client = Client(
        prepared.account_sid,
        prepared.auth_token,
        **client_kwargs,
    )
    if on_provider_call_start is not None:
        on_provider_call_start()
    message = client.messages.create(**dict(prepared.params))
    return _clean(getattr(message, "sid", None)) or None


__all__ = [
    "PreparedTenantTwilioMessage",
    "TenantTwilioMessagePreflight",
    "TenantTwilioScopeError",
    "TenantTwilioSenderSnapshot",
    "build_tenant_twilio_sender_binding",
    "prepare_bound_tenant_twilio_message",
    "resolve_tenant_twilio_sender_snapshot",
    "send_prepared_tenant_twilio_message",
]
