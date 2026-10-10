"""Durable WhatsApp intake and ordered outbound outbox primitives.

The webhook boundary should persist an inbound turn before acknowledging the
provider. Workers claim turns with compare-and-swap leases, process only the
oldest unfinished item in a stream, and atomically stage outbound attempts with
the completed turn. Provider sends use a separate lease. An expired ``sending``
lease is quarantined as ``send_uncertain`` and is never automatically retried.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import uuid
from typing import Any, Optional

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from database import db
from models import (
    ChannelSessionIdentityBinding,
    ChatSessionContext,
    ProviderConnection,
    ProviderSender,
    WhatsAppInboundTurn,
    WhatsAppOutboundAttempt,
)
from services.outbox_execution_budget import outbox_persistence_operation


INBOUND_CONTRACT_VERSION = WhatsAppInboundTurn.CONTRACT_VERSION
OUTBOUND_CONTRACT_VERSION = WhatsAppOutboundAttempt.CONTRACT_VERSION
HEALTH_CONTRACT_VERSION = "whatsapp.turn_health.v1"
PAYLOAD_SCRUB_CONTRACT_VERSION = "whatsapp.inbound_payload_scrub.v1"
PAYLOAD_SCRUB_RUN_CONTRACT_VERSION = "whatsapp.inbound_payload_scrub_run.v1"

INGEST_CREATED = "created"
INGEST_DUPLICATE = "duplicate"

DEFAULT_INBOUND_MAX_ATTEMPTS = 8
DEFAULT_OUTBOUND_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 120
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 60 * 60
MAX_DELAY_SECONDS = 30 * 24 * 60 * 60
MAX_CANDIDATE_LIMIT = 500

MAX_INBOUND_FIELDS = 64
MAX_INBOUND_PAYLOAD_BYTES = 64 * 1024
MAX_OUTBOUND_PAYLOAD_BYTES = 64 * 1024
MAX_RESULT_BYTES = 32 * 1024
MAX_FLOW_SOURCE_BYTES = 32 * 1024
MAX_BODY_BYTES = 8 * 1024
MAX_URL_BYTES = 4096
MAX_FIELD_BYTES = 2048
MAX_MEDIA_ITEMS = 10
DEFAULT_DEAD_PAYLOAD_RETENTION_HOURS = 72
MAX_DEAD_PAYLOAD_RETENTION_HOURS = 30 * 24
DEFAULT_PAYLOAD_SCRUB_BATCH_SIZE = 200
MAX_PAYLOAD_SCRUB_BATCH_SIZE = 500

_PAYLOAD_SCRUB_MARKER = "_chatboc_payload_scrub"

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")
_MEDIA_FIELD = re.compile(r"^(?:MediaUrl|MediaContentType|MediaSid)([0-9])$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")

_INBOUND_FIELDS = frozenset(
    {
        "AccountSid",
        "ApiVersion",
        "Body",
        "ButtonPayload",
        "ButtonText",
        "ChannelMetadata",
        "FlowData",
        "Forwarded",
        "FrequentlyForwarded",
        "From",
        "InteractiveData",
        "Latitude",
        "ListId",
        "ListTitle",
        "Longitude",
        "MessageSid",
        "MessagingServiceSid",
        "NumMedia",
        "OriginalRepliedMessageSid",
        "ProfileName",
        "ReferralBody",
        "ReferralHeadline",
        "ReferralMediaContentType",
        "ReferralMediaId",
        "ReferralMediaUrl",
        "ReferralNumMedia",
        "ReferralSourceId",
        "ReferralSourceType",
        "ReferralSourceUrl",
        "ServiceSid",
        "SmsMessageSid",
        "SmsStatus",
        "To",
        "WaId",
        # The synchronous signature/token boundary may provide the already
        # validated, persistence-safe contract under this internal key.
        "safe_flow_submission",
    }
)

_OUTBOUND_FIELDS = frozenset(
    {
        "_chatboc_policy_metadata",
        "body",
        "content_sid",
        "content_variables",
        "from_",
        "media_url",
        "messaging_service_sid",
        "persistent_action",
        "status_callback",
        "to",
    }
)

_SENSITIVE_KEYS = frozenset(
    {
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "cvv",
    "cvc",
    "pin",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_authorization",
    "_credential",
    "_credentials",
    "_password",
    "_secret",
    "_token",
    "_api_key",
    "_apikey",
    "_cvv",
    "_cvc",
    "_pin",
)


class WhatsAppTurnValidationError(ValueError):
    """Safe validation error; ``code`` never contains provider payload data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class WhatsAppInboundDigestConflict(RuntimeError):
    """The same provider identity was replayed with different immutable input."""

    def __init__(self, code: str = "inbound_payload_digest_conflict"):
        self.code = code
        super().__init__(code)


class WhatsAppTurnStateError(RuntimeError):
    """Persisted state violates the durable turn contract."""


@dataclass(frozen=True)
class InboundTurnReceipt:
    outcome: str
    database_id: int
    turn_id: str
    tenant_id: int
    provider: str
    provider_message_sid: str
    stream_key: str
    payload_digest: str
    message_kind: str
    status: str
    session_identity_binding_id: Optional[int] = None
    session_identity_version: Optional[str] = None
    session_identity_hmac: Optional[str] = None
    chat_session_id: Optional[str] = None

    @property
    def created(self) -> bool:
        return self.outcome == INGEST_CREATED


@dataclass(frozen=True)
class InboundTurnClaim:
    database_id: int
    turn_id: str
    tenant_id: int
    provider: str
    provider_message_sid: str
    provider_connection_id: Optional[int]
    provider_sender_id: Optional[int]
    stream_key: str
    message_kind: str
    payload: dict[str, Any]
    payload_digest: str
    attempt_count: int
    max_attempts: int
    lease_token: str
    leased_until: datetime
    session_identity_binding_id: Optional[int] = None
    session_identity_version: Optional[str] = None
    session_identity_hmac: Optional[str] = None
    chat_session_id: Optional[str] = None


@dataclass(frozen=True)
class InboundTurnCompletion:
    completed: bool
    turn_id: str
    outbound_attempt_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutboundAttemptClaim:
    database_id: int
    attempt_id: str
    tenant_id: int
    inbound_turn_id: int
    provider_connection_id: Optional[int]
    provider_sender_id: Optional[int]
    provider: str
    stream_key: str
    sequence_no: int
    message_kind: str
    idempotency_key: str
    payload: dict[str, Any]
    payload_digest: str
    attempt_count: int
    max_attempts: int
    lease_token: str
    leased_until: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: Optional[datetime]) -> datetime:
    current = value or _utc_now()
    if not isinstance(current, datetime):
        raise WhatsAppTurnValidationError("invalid_timestamp")
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _positive_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool):
        raise WhatsAppTurnValidationError(code)
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppTurnValidationError(code) from exc
    if normalized < 1:
        raise WhatsAppTurnValidationError(code)
    return normalized


def _bounded_attempts(value: Any, *, default: int) -> int:
    if value is None:
        return default
    normalized = _positive_int(value, code="invalid_max_attempts")
    if normalized > 32:
        raise WhatsAppTurnValidationError("invalid_max_attempts")
    return normalized


def _bounded_lease_seconds(value: Any) -> int:
    normalized = _positive_int(value, code="invalid_lease_seconds")
    if normalized > 60 * 60:
        raise WhatsAppTurnValidationError("invalid_lease_seconds")
    return normalized


def _bounded_nonnegative_int(
    value: Any,
    *,
    code: str,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise WhatsAppTurnValidationError(code)
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppTurnValidationError(code) from exc
    if normalized < 0 or normalized > maximum:
        raise WhatsAppTurnValidationError(code)
    return normalized


def _normalize_identifier(
    value: Any,
    *,
    code: str,
    max_length: int,
    lowercase: bool = False,
) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WhatsAppTurnValidationError(code)
    normalized = str(value).strip()
    if lowercase:
        normalized = normalized.lower()
    if (
        not normalized
        or len(normalized) > max_length
        or not _SAFE_IDENTIFIER.fullmatch(normalized)
    ):
        raise WhatsAppTurnValidationError(code)
    return normalized


def _normalize_digest(value: Any, *, code: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _HEX_DIGEST.fullmatch(normalized):
        raise WhatsAppTurnValidationError(code)
    return normalized


def _canonical_json(value: Any, *, max_bytes: int, error_code: str) -> tuple[str, str]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppTurnValidationError(error_code) from exc
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > max_bytes:
        raise WhatsAppTurnValidationError(error_code)
    return encoded, hashlib.sha256(encoded_bytes).hexdigest()


def _scalar_text(value: Any, *, max_bytes: int, code: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        rendered = str(value)
    else:
        raise WhatsAppTurnValidationError(code)
    if len(rendered.encode("utf-8")) > max_bytes:
        raise WhatsAppTurnValidationError(code)
    return rendered


def _is_sensitive_key(value: Any) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    return key in _SENSITIVE_KEYS or key.endswith(_SENSITIVE_KEY_SUFFIXES)


def _bounded_safe_json(
    value: Any,
    *,
    depth: int = 0,
    nodes: Optional[list[int]] = None,
) -> Any:
    if depth > 6:
        raise WhatsAppTurnValidationError("payload_too_complex")
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 256:
        raise WhatsAppTurnValidationError("payload_too_complex")

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise WhatsAppTurnValidationError("payload_invalid_number")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_FLOW_SOURCE_BYTES:
            raise WhatsAppTurnValidationError("payload_string_too_large")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_INBOUND_FIELDS:
            raise WhatsAppTurnValidationError("payload_field_count_exceeded")
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key or "").strip()
            if not key or len(key) > 80:
                raise WhatsAppTurnValidationError("payload_invalid_key")
            normalized[key] = (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _bounded_safe_json(child, depth=depth + 1, nodes=nodes)
            )
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise WhatsAppTurnValidationError("payload_list_too_large")
        return [
            _bounded_safe_json(child, depth=depth + 1, nodes=nodes)
            for child in value
        ]
    raise WhatsAppTurnValidationError("payload_invalid_type")


def _safe_flow_source(value: Any, *, field: str) -> str:
    rendered = _scalar_text(
        value,
        max_bytes=MAX_FLOW_SOURCE_BYTES,
        code="flow_payload_too_large",
    )
    if not rendered:
        return rendered
    try:
        parsed = json.loads(rendered)
    except (TypeError, ValueError):
        # Persist no opaque token-bearing Flow envelope. The synchronous flow
        # validator should provide ``safe_flow_submission`` instead.
        return "[REDACTED_INVALID_FLOW_PAYLOAD]"
    safe = _bounded_safe_json(parsed)
    encoded, _ = _canonical_json(
        safe,
        max_bytes=MAX_FLOW_SOURCE_BYTES,
        error_code=f"{field.lower()}_too_large",
    )
    return encoded


def normalize_whatsapp_inbound_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded provider allowlist; unknown fields are discarded."""

    if not isinstance(payload, Mapping):
        raise WhatsAppTurnValidationError("inbound_payload_must_be_object")
    if len(payload) > MAX_INBOUND_FIELDS:
        raise WhatsAppTurnValidationError("inbound_payload_field_count_exceeded")

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in payload.items():
        key = str(raw_key or "").strip()
        media_match = _MEDIA_FIELD.fullmatch(key)
        if key not in _INBOUND_FIELDS and media_match is None:
            continue
        if media_match is not None and int(media_match.group(1)) >= MAX_MEDIA_ITEMS:
            continue

        if key == "safe_flow_submission":
            if raw_value is not None:
                normalized[key] = _bounded_safe_json(raw_value)
            continue
        if key in {"FlowData", "InteractiveData"}:
            normalized[key] = _safe_flow_source(raw_value, field=key)
            continue
        if key == "Body":
            limit = MAX_BODY_BYTES
        elif "Url" in key or key == "status_callback":
            limit = MAX_URL_BYTES
        else:
            limit = MAX_FIELD_BYTES
        normalized[key] = _scalar_text(
            raw_value,
            max_bytes=limit,
            code="inbound_payload_field_too_large",
        )

    _canonical_json(
        normalized,
        max_bytes=MAX_INBOUND_PAYLOAD_BYTES,
        error_code="inbound_payload_too_large",
    )
    return normalized


def _infer_message_kind(payload: Mapping[str, Any]) -> str:
    # Keep the durable discriminator aligned with the signed webhook boundary.
    # Rich kinds such as sticker and emoji map onto the intentionally smaller
    # persisted enum (image and text respectively); event-only callbacks are
    # rejected before this function can create a conversational turn.
    from services.whatsapp_inbound_content import classify_twilio_whatsapp_payload

    return classify_twilio_whatsapp_payload(payload).durable_message_kind


def derive_whatsapp_stream_key(
    *,
    secret: str | bytes,
    tenant_id: Any,
    sender: Any,
    destination: Any,
) -> str:
    """Derive a non-PII FIFO key for one tenant/sender/destination stream."""

    tenant = _positive_int(tenant_id, code="invalid_tenant_id")
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret or b"")
    if len(secret_bytes) < 16:
        raise WhatsAppTurnValidationError("stream_key_secret_too_short")
    sender_text = str(sender or "").strip().lower()
    destination_text = str(destination or "").strip().lower()
    if not sender_text or not destination_text:
        raise WhatsAppTurnValidationError("stream_identity_missing")
    material = f"whatsapp-stream:v1:{tenant}:{sender_text}:{destination_text}".encode("utf-8")
    return hmac.new(secret_bytes, material, hashlib.sha256).hexdigest()


def _validate_provider_scope(
    session: Session,
    *,
    tenant_id: int,
    provider_connection_id: Optional[int],
    provider_sender_id: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    connection_id = None
    sender_id = None
    if provider_connection_id is not None:
        connection_id = _positive_int(
            provider_connection_id,
            code="invalid_provider_connection_id",
        )
        exists_connection = session.scalar(
            select(ProviderConnection.id).where(
                ProviderConnection.id == connection_id,
                ProviderConnection.tenant_id == tenant_id,
            )
        )
        if exists_connection is None:
            raise WhatsAppTurnValidationError("provider_connection_tenant_mismatch")

    if provider_sender_id is not None:
        sender_id = _positive_int(provider_sender_id, code="invalid_provider_sender_id")
        sender = session.scalar(
            select(ProviderSender).where(
                ProviderSender.id == sender_id,
                ProviderSender.tenant_id == tenant_id,
            )
        )
        if sender is None:
            raise WhatsAppTurnValidationError("provider_sender_tenant_mismatch")
        if (
            connection_id is not None
            and sender.provider_connection_id is not None
            and int(sender.provider_connection_id) != connection_id
        ):
            raise WhatsAppTurnValidationError("provider_sender_connection_mismatch")
    return connection_id, sender_id


def _receipt(turn: WhatsAppInboundTurn, outcome: str) -> InboundTurnReceipt:
    return InboundTurnReceipt(
        outcome=outcome,
        database_id=int(turn.id),
        turn_id=str(turn.turn_id),
        tenant_id=int(turn.tenant_id),
        provider=str(turn.provider),
        provider_message_sid=str(turn.provider_message_sid),
        stream_key=str(turn.stream_key),
        payload_digest=str(turn.payload_digest),
        message_kind=str(turn.message_kind),
        status=str(turn.status),
        session_identity_binding_id=turn.session_identity_binding_id,
        session_identity_version=(
            str(turn.session_identity_version)
            if turn.session_identity_version
            else None
        ),
        session_identity_hmac=(
            str(turn.session_identity_hmac)
            if turn.session_identity_hmac
            else None
        ),
        chat_session_id=str(turn.chat_session_id) if turn.chat_session_id else None,
    )


def _assert_replay_matches(
    turn: WhatsAppInboundTurn,
    *,
    payload_digest: str,
    stream_key: str,
    provider_connection_id: Optional[int],
    provider_sender_id: Optional[int],
    session_identity_binding_id: Optional[int],
    session_identity_version: Optional[str],
    session_identity_hmac: Optional[str],
    chat_session_id: Optional[str],
) -> None:
    if not hmac.compare_digest(str(turn.payload_digest or ""), payload_digest):
        raise WhatsAppInboundDigestConflict()
    if str(turn.stream_key or "") != stream_key:
        raise WhatsAppInboundDigestConflict("inbound_stream_scope_conflict")
    if turn.provider_connection_id != provider_connection_id:
        raise WhatsAppInboundDigestConflict("inbound_connection_scope_conflict")
    if turn.provider_sender_id != provider_sender_id:
        raise WhatsAppInboundDigestConflict("inbound_sender_scope_conflict")
    if turn.session_identity_binding_id != session_identity_binding_id:
        raise WhatsAppInboundDigestConflict("inbound_session_identity_scope_conflict")
    if (turn.session_identity_version or None) != session_identity_version:
        raise WhatsAppInboundDigestConflict("inbound_session_identity_version_conflict")
    if not hmac.compare_digest(
        str(turn.session_identity_hmac or ""),
        str(session_identity_hmac or ""),
    ):
        raise WhatsAppInboundDigestConflict("inbound_session_identity_hmac_conflict")
    if (turn.chat_session_id or None) != chat_session_id:
        raise WhatsAppInboundDigestConflict("inbound_chat_session_scope_conflict")


def _validate_session_identity_scope(
    session: Session,
    *,
    tenant_id: int,
    provider: str,
    binding_id: Any,
    identity_version: Any,
    identity_hmac: Any,
    chat_session_id: Any,
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    provided = (
        binding_id is not None,
        identity_version not in (None, ""),
        identity_hmac not in (None, ""),
        chat_session_id not in (None, ""),
    )
    if not any(provided):
        return None, None, None, None
    if not all(provided):
        raise WhatsAppTurnValidationError("session_identity_scope_incomplete")
    resolved_binding_id = _positive_int(
        binding_id,
        code="invalid_session_identity_binding_id",
    )
    resolved_version = _normalize_identifier(
        identity_version,
        code="invalid_session_identity_version",
        max_length=32,
        lowercase=True,
    )
    resolved_hmac = _normalize_digest(
        identity_hmac,
        code="invalid_session_identity_hmac",
    )
    resolved_chat_session_id = _normalize_identifier(
        chat_session_id,
        code="invalid_chat_session_id",
        max_length=36,
        lowercase=True,
    )
    binding = session.scalar(
        select(ChannelSessionIdentityBinding).where(
            ChannelSessionIdentityBinding.id == resolved_binding_id,
            ChannelSessionIdentityBinding.tenant_id == tenant_id,
        )
    )
    if binding is None:
        raise WhatsAppTurnValidationError("session_identity_binding_tenant_mismatch")
    context = session.get(ChatSessionContext, binding.chat_session_id)
    if (
        binding.status != ChannelSessionIdentityBinding.STATUS_ACTIVE
        or str(binding.channel) != "whatsapp"
        or str(binding.provider) != provider
        or str(binding.identity_version) != resolved_version
        or not hmac.compare_digest(str(binding.identity_hmac), resolved_hmac)
        or str(binding.chat_session_id) != resolved_chat_session_id
        or context is None
        or context.tenant_id is None
        or int(context.tenant_id) != tenant_id
    ):
        raise WhatsAppTurnValidationError("session_identity_binding_scope_mismatch")
    return (
        resolved_binding_id,
        resolved_version,
        resolved_hmac,
        resolved_chat_session_id,
    )


def _is_scrubbed_inbound_payload(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    marker = payload.get(_PAYLOAD_SCRUB_MARKER)
    return bool(
        isinstance(marker, Mapping)
        and marker.get("contract_version") == PAYLOAD_SCRUB_CONTRACT_VERSION
        and marker.get("retained_payload") is False
    )


def _scrubbed_inbound_payload(
    turn: WhatsAppInboundTurn,
    *,
    reason: str,
    scrubbed_at: datetime,
) -> dict[str, Any]:
    """Build a non-PII tombstone while preserving replay evidence."""

    original_payload = turn.payload_json if isinstance(turn.payload_json, Mapping) else {}
    encoded, _ = _canonical_json(
        original_payload,
        max_bytes=MAX_INBOUND_PAYLOAD_BYTES,
        error_code="inbound_payload_too_large",
    )
    return {
        _PAYLOAD_SCRUB_MARKER: {
            "contract_version": PAYLOAD_SCRUB_CONTRACT_VERSION,
            "payload_digest_sha256": str(turn.payload_digest),
            "message_kind": str(turn.message_kind),
            "original_bytes": len(encoded.encode("utf-8")),
            "original_fields": len(original_payload),
            "reason": reason,
            "retained_payload": False,
            "scrubbed_at": _coerce_utc(scrubbed_at).isoformat(),
        }
    }


def ingest_whatsapp_inbound_turn(
    *,
    tenant_id: Any,
    provider_message_sid: Any,
    stream_key: Any,
    payload: Mapping[str, Any],
    provider: Any = "twilio",
    provider_connection_id: Any = None,
    provider_sender_id: Any = None,
    conversation_id: Any = None,
    channel_session_id: Any = None,
    chat_session_id: Any = None,
    session_identity_binding_id: Any = None,
    session_identity_version: Any = None,
    session_identity_hmac: Any = None,
    payload_digest: Any = None,
    max_attempts: Any = DEFAULT_INBOUND_MAX_ATTEMPTS,
    received_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> InboundTurnReceipt:
    """Persist or replay one inbound event in a short independent transaction."""

    resolved_tenant_id = _positive_int(tenant_id, code="invalid_tenant_id")
    resolved_provider = _normalize_identifier(
        provider,
        code="invalid_provider",
        max_length=32,
        lowercase=True,
    )
    resolved_message_sid = _normalize_identifier(
        provider_message_sid,
        code="invalid_provider_message_sid",
        max_length=180,
    )
    resolved_stream_key = _normalize_identifier(
        stream_key,
        code="invalid_stream_key",
        max_length=128,
    )
    normalized_payload = normalize_whatsapp_inbound_payload(payload)
    payload_sid_values = {
        str(normalized_payload.get(field) or "").strip()
        for field in ("MessageSid", "SmsMessageSid")
        if normalized_payload.get(field) not in (None, "")
    }
    if payload_sid_values and payload_sid_values != {resolved_message_sid}:
        raise WhatsAppTurnValidationError("provider_message_sid_mismatch")
    _, calculated_digest = _canonical_json(
        normalized_payload,
        max_bytes=MAX_INBOUND_PAYLOAD_BYTES,
        error_code="inbound_payload_too_large",
    )
    resolved_digest = (
        _normalize_digest(payload_digest, code="invalid_payload_digest")
        if payload_digest not in (None, "")
        else calculated_digest
    )
    if not hmac.compare_digest(resolved_digest, calculated_digest):
        raise WhatsAppTurnValidationError("payload_digest_mismatch")
    resolved_max_attempts = _bounded_attempts(
        max_attempts,
        default=DEFAULT_INBOUND_MAX_ATTEMPTS,
    )
    operation_now = _coerce_utc(now)
    original_received_at = (
        _coerce_utc(received_at) if received_at is not None else operation_now
    )

    with Session(bind=db.engine, expire_on_commit=False) as session:
        connection_id, sender_id = _validate_provider_scope(
            session,
            tenant_id=resolved_tenant_id,
            provider_connection_id=provider_connection_id,
            provider_sender_id=provider_sender_id,
        )
        (
            resolved_identity_binding_id,
            resolved_identity_version,
            resolved_identity_hmac,
            resolved_chat_session_id,
        ) = _validate_session_identity_scope(
            session,
            tenant_id=resolved_tenant_id,
            provider=resolved_provider,
            binding_id=session_identity_binding_id,
            identity_version=session_identity_version,
            identity_hmac=session_identity_hmac,
            chat_session_id=chat_session_id,
        )
        existing = session.scalar(
            select(WhatsAppInboundTurn).where(
                WhatsAppInboundTurn.tenant_id == resolved_tenant_id,
                WhatsAppInboundTurn.provider == resolved_provider,
                WhatsAppInboundTurn.provider_message_sid == resolved_message_sid,
            )
        )
        if existing is not None:
            _assert_replay_matches(
                existing,
                payload_digest=resolved_digest,
                stream_key=resolved_stream_key,
                provider_connection_id=connection_id,
                provider_sender_id=sender_id,
                session_identity_binding_id=resolved_identity_binding_id,
                session_identity_version=resolved_identity_version,
                session_identity_hmac=resolved_identity_hmac,
                chat_session_id=resolved_chat_session_id,
            )
            result = _receipt(existing, INGEST_DUPLICATE)
            session.rollback()
            return result

        turn = WhatsAppInboundTurn(
            turn_id=str(uuid.uuid4()),
            tenant_id=resolved_tenant_id,
            provider_connection_id=connection_id,
            provider_sender_id=sender_id,
            provider=resolved_provider,
            provider_message_sid=resolved_message_sid,
            stream_key=resolved_stream_key,
            conversation_id=str(conversation_id).strip()[:36] if conversation_id else None,
            channel_session_id=(
                _positive_int(channel_session_id, code="invalid_channel_session_id")
                if channel_session_id is not None
                else None
            ),
            chat_session_id=(
                resolved_chat_session_id
                if resolved_identity_binding_id is not None
                else str(chat_session_id).strip()[:64] if chat_session_id else None
            ),
            session_identity_binding_id=resolved_identity_binding_id,
            session_identity_version=resolved_identity_version,
            session_identity_hmac=resolved_identity_hmac,
            message_kind=_infer_message_kind(normalized_payload),
            payload_digest=resolved_digest,
            payload_json=normalized_payload,
            status=WhatsAppInboundTurn.STATUS_RECEIVED,
            attempt_count=0,
            max_attempts=resolved_max_attempts,
            available_at=operation_now,
            # A buffered provider event may be persisted after a newer event.
            # Keep its original receipt time so stream ordering survives replay;
            # created_at/available_at still describe the local ingest attempt.
            received_at=original_received_at,
            contract_version=INBOUND_CONTRACT_VERSION,
            created_at=operation_now,
            updated_at=operation_now,
        )
        session.add(turn)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(WhatsAppInboundTurn).where(
                    WhatsAppInboundTurn.tenant_id == resolved_tenant_id,
                    WhatsAppInboundTurn.provider == resolved_provider,
                    WhatsAppInboundTurn.provider_message_sid == resolved_message_sid,
                )
            )
            if existing is None:
                raise
            _assert_replay_matches(
                existing,
                payload_digest=resolved_digest,
                stream_key=resolved_stream_key,
                provider_connection_id=connection_id,
                provider_sender_id=sender_id,
                session_identity_binding_id=resolved_identity_binding_id,
                session_identity_version=resolved_identity_version,
                session_identity_hmac=resolved_identity_hmac,
                chat_session_id=resolved_chat_session_id,
            )
            return _receipt(existing, INGEST_DUPLICATE)
        return _receipt(turn, INGEST_CREATED)


def _inbound_due_filter(now: datetime):
    return or_(
        and_(
            WhatsAppInboundTurn.status.in_(
                [
                    WhatsAppInboundTurn.STATUS_RECEIVED,
                    WhatsAppInboundTurn.STATUS_RETRY_WAIT,
                ]
            ),
            WhatsAppInboundTurn.attempt_count < WhatsAppInboundTurn.max_attempts,
            WhatsAppInboundTurn.available_at <= now,
        ),
        and_(
            WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
            WhatsAppInboundTurn.leased_until.isnot(None),
            WhatsAppInboundTurn.leased_until <= now,
            WhatsAppInboundTurn.attempt_count < WhatsAppInboundTurn.max_attempts,
        ),
    )


def _quarantine_exhausted_inbound_leases(
    session: Session,
    *,
    now: datetime,
    tenant_id: Optional[int] = None,
    stream_key: Optional[str] = None,
) -> int:
    """Terminate crashed turns that exhausted their bounded processing budget."""

    statement = update(WhatsAppInboundTurn).where(
            WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
            WhatsAppInboundTurn.leased_until.isnot(None),
            WhatsAppInboundTurn.leased_until <= now,
            WhatsAppInboundTurn.attempt_count >= WhatsAppInboundTurn.max_attempts,
        )
    if tenant_id is not None:
        statement = statement.where(WhatsAppInboundTurn.tenant_id == tenant_id)
    if stream_key is not None:
        statement = statement.where(WhatsAppInboundTurn.stream_key == stream_key)
    transition = session.execute(
        statement.values(
            status=WhatsAppInboundTurn.STATUS_DEAD,
            lease_token=None,
            leased_until=None,
            completed_at=now,
            last_error_code="processing_lease_expired",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return int(transition.rowcount or 0)


def claim_next_whatsapp_inbound_turn(
    *,
    tenant_id: Any = None,
    stream_key: Any = None,
    lease_seconds: Any = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Optional[InboundTurnClaim]:
    """Claim the oldest unfinished due turn; later turns in its stream wait."""

    operation_now = _coerce_utc(now)
    lease_duration = _bounded_lease_seconds(lease_seconds)
    resolved_tenant_id = (
        _positive_int(tenant_id, code="invalid_tenant_id")
        if tenant_id is not None
        else None
    )
    resolved_stream_key = (
        _normalize_identifier(
            stream_key,
            code="invalid_stream_key",
            max_length=128,
        )
        if stream_key is not None
        else None
    )

    older = aliased(WhatsAppInboundTurn)
    head_of_stream = ~exists().where(
        older.tenant_id == WhatsAppInboundTurn.tenant_id,
        older.stream_key == WhatsAppInboundTurn.stream_key,
        or_(
            older.received_at < WhatsAppInboundTurn.received_at,
            and_(
                older.received_at == WhatsAppInboundTurn.received_at,
                older.id < WhatsAppInboundTurn.id,
            ),
        ),
        older.status.notin_(
            [WhatsAppInboundTurn.STATUS_COMPLETED, WhatsAppInboundTurn.STATUS_DEAD]
        ),
    )

    with Session(bind=db.engine, expire_on_commit=False) as session:
        exhausted = _quarantine_exhausted_inbound_leases(
            session,
            now=operation_now,
            tenant_id=resolved_tenant_id,
            stream_key=resolved_stream_key,
        )
        if exhausted:
            session.commit()
        candidate_query = select(WhatsAppInboundTurn.id).where(
            _inbound_due_filter(operation_now),
            head_of_stream,
        )
        if resolved_tenant_id is not None:
            candidate_query = candidate_query.where(
                WhatsAppInboundTurn.tenant_id == resolved_tenant_id
            )
        if resolved_stream_key is not None:
            candidate_query = candidate_query.where(
                WhatsAppInboundTurn.stream_key == resolved_stream_key
            )
        candidate_ids = list(
            session.scalars(
                candidate_query.order_by(
                    WhatsAppInboundTurn.received_at.asc(),
                    WhatsAppInboundTurn.id.asc(),
                ).limit(MAX_CANDIDATE_LIMIT)
            )
        )

        for candidate_id in candidate_ids:
            token = secrets.token_hex(24)
            leased_until = operation_now + timedelta(seconds=lease_duration)
            transition = session.execute(
                update(WhatsAppInboundTurn)
                .where(
                    WhatsAppInboundTurn.id == int(candidate_id),
                    _inbound_due_filter(operation_now),
                )
                .values(
                    status=WhatsAppInboundTurn.STATUS_PROCESSING,
                    # An expired lease is a real failed attempt. Incrementing
                    # it prevents a crashing worker from being reclaimed
                    # forever without ever reaching the dead-letter state.
                    attempt_count=WhatsAppInboundTurn.attempt_count + 1,
                    lease_token=token,
                    leased_until=leased_until,
                    processing_started_at=operation_now,
                    completed_at=None,
                    last_error_code=None,
                    updated_at=operation_now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(transition.rowcount or 0) != 1:
                session.rollback()
                continue
            try:
                session.commit()
            except IntegrityError:
                # The partial unique index fences another active worker in the
                # same stream even if both selected from a stale snapshot.
                session.rollback()
                continue
            turn = session.get(
                WhatsAppInboundTurn,
                int(candidate_id),
                populate_existing=True,
            )
            if (
                turn is None
                or turn.status != WhatsAppInboundTurn.STATUS_PROCESSING
                or turn.lease_token != token
            ):
                session.rollback()
                continue
            return InboundTurnClaim(
                database_id=int(turn.id),
                turn_id=str(turn.turn_id),
                tenant_id=int(turn.tenant_id),
                provider=str(turn.provider),
                provider_message_sid=str(turn.provider_message_sid),
                provider_connection_id=turn.provider_connection_id,
                provider_sender_id=turn.provider_sender_id,
                stream_key=str(turn.stream_key),
                message_kind=str(turn.message_kind),
                payload=dict(turn.payload_json or {}),
                payload_digest=str(turn.payload_digest),
                attempt_count=int(turn.attempt_count),
                max_attempts=int(turn.max_attempts),
                lease_token=token,
                leased_until=_coerce_utc(turn.leased_until),
                session_identity_binding_id=turn.session_identity_binding_id,
                session_identity_version=(
                    str(turn.session_identity_version)
                    if turn.session_identity_version
                    else None
                ),
                session_identity_hmac=(
                    str(turn.session_identity_hmac)
                    if turn.session_identity_hmac
                    else None
                ),
                chat_session_id=(
                    str(turn.chat_session_id) if turn.chat_session_id else None
                ),
            )
    return None


def renew_whatsapp_inbound_turn_lease(
    turn_id: Any,
    lease_token: Any,
    *,
    lease_seconds: Any = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Extend a live fenced lease; stale workers cannot resurrect their lease."""

    resolved_turn_id = _normalize_identifier(
        turn_id,
        code="invalid_turn_id",
        max_length=36,
        lowercase=True,
    )
    resolved_lease_token = _normalize_identifier(
        lease_token,
        code="invalid_lease_token",
        max_length=64,
        lowercase=True,
    )
    operation_now = _coerce_utc(now)
    renewed_until = operation_now + timedelta(
        seconds=_bounded_lease_seconds(lease_seconds)
    )
    with Session(bind=db.engine, expire_on_commit=False) as session:
        transition = session.execute(
            update(WhatsAppInboundTurn)
            .where(
                WhatsAppInboundTurn.turn_id == resolved_turn_id,
                WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
                WhatsAppInboundTurn.lease_token == resolved_lease_token,
                WhatsAppInboundTurn.leased_until.isnot(None),
                WhatsAppInboundTurn.leased_until > operation_now,
            )
            .values(leased_until=renewed_until, updated_at=operation_now)
            .execution_options(synchronize_session=False)
        )
        if int(transition.rowcount or 0) != 1:
            session.rollback()
            return None
        session.commit()
        return renewed_until


def normalize_whatsapp_outbound_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise WhatsAppTurnValidationError("outbound_payload_must_be_object")
    unknown = {str(key) for key in payload}.difference(_OUTBOUND_FIELDS)
    if unknown:
        raise WhatsAppTurnValidationError("outbound_payload_field_not_allowed")

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if key in {"media_url", "persistent_action"}:
            items = value if isinstance(value, (list, tuple)) else [value]
            if len(items) > MAX_MEDIA_ITEMS:
                raise WhatsAppTurnValidationError("outbound_payload_list_too_large")
            normalized[key] = [
                _scalar_text(
                    item,
                    max_bytes=MAX_URL_BYTES if key == "media_url" else MAX_BODY_BYTES,
                    code="outbound_payload_field_too_large",
                )
                for item in items
            ]
        elif key in {"content_variables", "_chatboc_policy_metadata"} and isinstance(
            value,
            Mapping,
        ):
            normalized[key] = _bounded_safe_json(value)
        else:
            limit = (
                MAX_BODY_BYTES
                if key == "body"
                else MAX_URL_BYTES
                if key == "status_callback"
                else MAX_FIELD_BYTES
            )
            normalized[key] = _scalar_text(
                value,
                max_bytes=limit,
                code="outbound_payload_field_too_large",
            )
    if not any(
        normalized.get(key)
        for key in ("body", "content_sid", "media_url", "persistent_action")
    ):
        raise WhatsAppTurnValidationError("outbound_payload_content_missing")
    _canonical_json(
        normalized,
        max_bytes=MAX_OUTBOUND_PAYLOAD_BYTES,
        error_code="outbound_payload_too_large",
    )
    return normalized


def _normalize_outbound_specs(
    turn: WhatsAppInboundTurn,
    outbound: Optional[Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for sequence_no, raw_spec in enumerate(outbound or (), start=1):
        if not isinstance(raw_spec, Mapping):
            raise WhatsAppTurnValidationError("outbound_spec_must_be_object")
        allowed_spec_fields = {
            "message_kind",
            "payload",
            "idempotency_key",
            "max_attempts",
            "delay_seconds",
        }
        if {str(key) for key in raw_spec}.difference(allowed_spec_fields):
            raise WhatsAppTurnValidationError("outbound_spec_field_not_allowed")
        message_kind = str(raw_spec.get("message_kind") or "").strip().lower()
        if message_kind not in {"text", "media", "audio", "template", "interactive"}:
            raise WhatsAppTurnValidationError("invalid_outbound_message_kind")
        payload = normalize_whatsapp_outbound_payload(raw_spec.get("payload") or {})
        _, payload_digest = _canonical_json(
            payload,
            max_bytes=MAX_OUTBOUND_PAYLOAD_BYTES,
            error_code="outbound_payload_too_large",
        )
        default_key = f"whatsapp-turn:{turn.turn_id}:out:{sequence_no}:{payload_digest[:16]}"
        idempotency_key = _normalize_identifier(
            raw_spec.get("idempotency_key") or default_key,
            code="invalid_outbound_idempotency_key",
            max_length=191,
        )
        raw_delay = raw_spec.get("delay_seconds", 0)
        if isinstance(raw_delay, bool):
            raise WhatsAppTurnValidationError("invalid_outbound_delay")
        try:
            delay_seconds = int(raw_delay or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WhatsAppTurnValidationError("invalid_outbound_delay") from exc
        if delay_seconds < 0 or delay_seconds > MAX_DELAY_SECONDS:
            raise WhatsAppTurnValidationError("invalid_outbound_delay")
        specs.append(
            {
                "attempt_id": str(uuid.uuid4()),
                "sequence_no": sequence_no,
                "message_kind": message_kind,
                "idempotency_key": idempotency_key,
                "payload": payload,
                "payload_digest": payload_digest,
                "delay_seconds": delay_seconds,
                "max_attempts": _bounded_attempts(
                    raw_spec.get("max_attempts"),
                    default=DEFAULT_OUTBOUND_MAX_ATTEMPTS,
                ),
            }
        )
    return specs


def _normalize_result(result: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise WhatsAppTurnValidationError("result_must_be_object")
    normalized = _bounded_safe_json(result)
    _canonical_json(normalized, max_bytes=MAX_RESULT_BYTES, error_code="result_too_large")
    return dict(normalized)


@outbox_persistence_operation
def complete_whatsapp_inbound_turn(
    turn_id: Any,
    lease_token: Any,
    *,
    result: Optional[Mapping[str, Any]] = None,
    outbound: Optional[Sequence[Mapping[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> InboundTurnCompletion:
    """Fenced completion that atomically stages the ordered outbound outbox."""

    resolved_turn_id = _normalize_identifier(
        turn_id,
        code="invalid_turn_id",
        max_length=36,
        lowercase=True,
    )
    resolved_lease_token = _normalize_identifier(
        lease_token,
        code="invalid_lease_token",
        max_length=64,
        lowercase=True,
    )
    operation_now = _coerce_utc(now)
    normalized_result = _normalize_result(result)

    with Session(bind=db.engine, expire_on_commit=False) as session:
        turn = session.scalar(
            select(WhatsAppInboundTurn).where(
                WhatsAppInboundTurn.turn_id == resolved_turn_id,
                WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
                WhatsAppInboundTurn.lease_token == resolved_lease_token,
            )
        )
        if turn is None:
            session.rollback()
            return InboundTurnCompletion(completed=False, turn_id=resolved_turn_id)
        specs = _normalize_outbound_specs(turn, outbound)
        scrubbed_payload = _scrubbed_inbound_payload(
            turn,
            reason="completed",
            scrubbed_at=operation_now,
        )

        transition = session.execute(
            update(WhatsAppInboundTurn)
            .where(
                WhatsAppInboundTurn.id == turn.id,
                WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
                WhatsAppInboundTurn.lease_token == resolved_lease_token,
            )
            .values(
                status=WhatsAppInboundTurn.STATUS_COMPLETED,
                lease_token=None,
                leased_until=None,
                completed_at=operation_now,
                last_error_code=None,
                result_json=normalized_result,
                payload_json=scrubbed_payload,
                updated_at=operation_now,
            )
            .execution_options(synchronize_session=False)
        )
        if int(transition.rowcount or 0) != 1:
            session.rollback()
            return InboundTurnCompletion(completed=False, turn_id=resolved_turn_id)

        rows: list[WhatsAppOutboundAttempt] = []
        for spec in specs:
            row = WhatsAppOutboundAttempt(
                attempt_id=spec["attempt_id"],
                tenant_id=turn.tenant_id,
                inbound_turn_id=turn.id,
                provider_connection_id=turn.provider_connection_id,
                provider_sender_id=turn.provider_sender_id,
                provider=turn.provider,
                stream_key=turn.stream_key,
                sequence_no=spec["sequence_no"],
                message_kind=spec["message_kind"],
                idempotency_key=spec["idempotency_key"],
                payload_digest=spec["payload_digest"],
                payload_json=spec["payload"],
                status=WhatsAppOutboundAttempt.STATUS_PENDING,
                provider_status="unknown",
                attempt_count=0,
                max_attempts=spec["max_attempts"],
                available_at=operation_now + timedelta(seconds=spec["delay_seconds"]),
                contract_version=OUTBOUND_CONTRACT_VERSION,
                created_at=operation_now,
                updated_at=operation_now,
            )
            session.add(row)
            rows.append(row)
        try:
            session.flush()
            attempt_ids = tuple(str(row.attempt_id) for row in rows)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return InboundTurnCompletion(
            completed=True,
            turn_id=resolved_turn_id,
            outbound_attempt_ids=attempt_ids,
        )


def _safe_error_details(error: Any) -> tuple[str, str]:
    if isinstance(error, BaseException):
        raw = type(error).__name__
    else:
        raw = str(error or "turn_processing_failed").strip()
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    code = raw if _SAFE_ERROR_CODE.fullmatch(raw) else f"error_sha256_{digest[:16]}"
    return code[:96], digest


def _retry_delay(attempt_count: int) -> int:
    exponent = max(0, min(int(attempt_count) - 1, 16))
    return min(BASE_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)


@outbox_persistence_operation
def _fail_whatsapp_inbound_turn(
    turn_id: Any,
    lease_token: Any,
    error: Any,
    *,
    permanent: bool,
    now: Optional[datetime] = None,
) -> Optional[str]:
    resolved_turn_id = _normalize_identifier(
        turn_id,
        code="invalid_turn_id",
        max_length=36,
        lowercase=True,
    )
    resolved_lease_token = _normalize_identifier(
        lease_token,
        code="invalid_lease_token",
        max_length=64,
        lowercase=True,
    )
    operation_now = _coerce_utc(now)
    error_code, _ = _safe_error_details(error)

    with Session(bind=db.engine, expire_on_commit=False) as session:
        turn = session.scalar(
            select(WhatsAppInboundTurn).where(
                WhatsAppInboundTurn.turn_id == resolved_turn_id,
                WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
                WhatsAppInboundTurn.lease_token == resolved_lease_token,
            )
        )
        if turn is None:
            session.rollback()
            return None
        dead = permanent or int(turn.attempt_count) >= int(turn.max_attempts)
        target = (
            WhatsAppInboundTurn.STATUS_DEAD
            if dead
            else WhatsAppInboundTurn.STATUS_RETRY_WAIT
        )
        available_at = (
            operation_now
            if dead
            else operation_now + timedelta(seconds=_retry_delay(turn.attempt_count))
        )
        transition = session.execute(
            update(WhatsAppInboundTurn)
            .where(
                WhatsAppInboundTurn.id == turn.id,
                WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
                WhatsAppInboundTurn.lease_token == resolved_lease_token,
            )
            .values(
                status=target,
                available_at=available_at,
                lease_token=None,
                leased_until=None,
                completed_at=operation_now if dead else None,
                last_error_code=error_code,
                updated_at=operation_now,
            )
            .execution_options(synchronize_session=False)
        )
        if int(transition.rowcount or 0) != 1:
            session.rollback()
            return None
        session.commit()
        return target


def retry_whatsapp_inbound_turn(
    turn_id: Any,
    lease_token: Any,
    error: Any,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    return _fail_whatsapp_inbound_turn(
        turn_id,
        lease_token,
        error,
        permanent=False,
        now=now,
    )


def dead_whatsapp_inbound_turn(
    turn_id: Any,
    lease_token: Any,
    error: Any,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    return _fail_whatsapp_inbound_turn(
        turn_id,
        lease_token,
        error,
        permanent=True,
        now=now,
    )


def scrub_expired_whatsapp_inbound_payloads(
    *,
    dead_retention_hours: Any = DEFAULT_DEAD_PAYLOAD_RETENTION_HOURS,
    legal_hold: Any = False,
    limit: Any = DEFAULT_PAYLOAD_SCRUB_BATCH_SIZE,
    tenant_id: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Scrub terminal inbound payloads in a bounded, payload-free batch."""

    if not isinstance(legal_hold, bool):
        # Ambiguous policy must preserve evidence instead of deleting it.
        raise WhatsAppTurnValidationError("invalid_payload_legal_hold")
    retention_hours = _bounded_nonnegative_int(
        dead_retention_hours,
        code="invalid_dead_payload_retention_hours",
        maximum=MAX_DEAD_PAYLOAD_RETENTION_HOURS,
    )
    batch_limit = _positive_int(limit, code="invalid_payload_scrub_batch_size")
    if batch_limit > MAX_PAYLOAD_SCRUB_BATCH_SIZE:
        raise WhatsAppTurnValidationError("invalid_payload_scrub_batch_size")
    resolved_tenant_id = (
        _positive_int(tenant_id, code="invalid_tenant_id")
        if tenant_id is not None
        else None
    )
    operation_now = _coerce_utc(now)
    dead_cutoff = operation_now - timedelta(hours=retention_hours)
    terminal_filter = or_(
        WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_COMPLETED,
        and_(
            WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_DEAD,
            WhatsAppInboundTurn.completed_at <= dead_cutoff,
        ),
    )
    unscrubbed_filter = WhatsAppInboundTurn.payload_json[
        _PAYLOAD_SCRUB_MARKER
    ].as_string().is_(None)

    with Session(bind=db.engine, expire_on_commit=False) as session:
        eligible_query = select(func.count(WhatsAppInboundTurn.id)).where(
            terminal_filter,
            unscrubbed_filter,
        )
        if resolved_tenant_id is not None:
            eligible_query = eligible_query.where(
                WhatsAppInboundTurn.tenant_id == resolved_tenant_id
            )
        eligible = int(session.scalar(eligible_query) or 0)
        if legal_hold:
            session.rollback()
            return {
                "contract_version": PAYLOAD_SCRUB_RUN_CONTRACT_VERSION,
                "status": "legal_hold",
                "tenant_id": resolved_tenant_id,
                "legal_hold": True,
                "retention_hours": retention_hours,
                "cutoff_at": dead_cutoff.isoformat(),
                "eligible": eligible,
                "selected": 0,
                "scrubbed": 0,
                "completed_scrubbed": 0,
                "dead_scrubbed": 0,
                "remaining_eligible": eligible,
            }

        candidate_query = select(WhatsAppInboundTurn).where(
            terminal_filter,
            unscrubbed_filter,
        )
        if resolved_tenant_id is not None:
            candidate_query = candidate_query.where(
                WhatsAppInboundTurn.tenant_id == resolved_tenant_id
            )
        candidates = list(
            session.scalars(
                candidate_query.order_by(
                    WhatsAppInboundTurn.completed_at.asc(),
                    WhatsAppInboundTurn.id.asc(),
                )
                .limit(batch_limit)
                .with_for_update(skip_locked=True)
            )
        )
        completed_scrubbed = 0
        dead_scrubbed = 0
        for turn in candidates:
            if _is_scrubbed_inbound_payload(turn.payload_json):
                continue
            reason = (
                "completed"
                if turn.status == WhatsAppInboundTurn.STATUS_COMPLETED
                else "dead_retention_expired"
            )
            turn.payload_json = _scrubbed_inbound_payload(
                turn,
                reason=reason,
                scrubbed_at=operation_now,
            )
            turn.updated_at = operation_now
            if turn.status == WhatsAppInboundTurn.STATUS_COMPLETED:
                completed_scrubbed += 1
            else:
                dead_scrubbed += 1
        session.flush()
        remaining_eligible = max(0, eligible - completed_scrubbed - dead_scrubbed)
        session.commit()
        scrubbed = completed_scrubbed + dead_scrubbed
        return {
            "contract_version": PAYLOAD_SCRUB_RUN_CONTRACT_VERSION,
            "status": "completed",
            "tenant_id": resolved_tenant_id,
            "legal_hold": False,
            "retention_hours": retention_hours,
            "cutoff_at": dead_cutoff.isoformat(),
            "eligible": eligible,
            "selected": len(candidates),
            "scrubbed": scrubbed,
            "completed_scrubbed": completed_scrubbed,
            "dead_scrubbed": dead_scrubbed,
            "remaining_eligible": remaining_eligible,
        }


def _quarantine_expired_outbound_sends(
    session: Session,
    *,
    now: datetime,
    tenant_id: Optional[int] = None,
) -> int:
    error_code, error_digest = _safe_error_details("send_lease_expired")
    statement = update(WhatsAppOutboundAttempt).where(
            WhatsAppOutboundAttempt.status == WhatsAppOutboundAttempt.STATUS_SENDING,
            WhatsAppOutboundAttempt.leased_until.isnot(None),
            WhatsAppOutboundAttempt.leased_until <= now,
        )
    if tenant_id is not None:
        statement = statement.where(WhatsAppOutboundAttempt.tenant_id == tenant_id)
    transition = session.execute(
        statement.values(
            status=WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN,
            lease_token=None,
            leased_until=None,
            completed_at=now,
            last_error_code=error_code,
            last_error_digest=error_digest,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return int(transition.rowcount or 0)


def claim_next_whatsapp_outbound_attempt(
    *,
    tenant_id: Any = None,
    lease_seconds: Any = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Optional[OutboundAttemptClaim]:
    """Claim an ordered send; expired sends become uncertain, never due again."""

    operation_now = _coerce_utc(now)
    lease_duration = _bounded_lease_seconds(lease_seconds)
    resolved_tenant_id = (
        _positive_int(tenant_id, code="invalid_tenant_id")
        if tenant_id is not None
        else None
    )
    earlier = aliased(WhatsAppOutboundAttempt)
    no_blocking_predecessor = ~exists().where(
        earlier.tenant_id == WhatsAppOutboundAttempt.tenant_id,
        earlier.stream_key == WhatsAppOutboundAttempt.stream_key,
        earlier.id < WhatsAppOutboundAttempt.id,
        earlier.status.notin_(
            [
                WhatsAppOutboundAttempt.STATUS_ACCEPTED,
                WhatsAppOutboundAttempt.STATUS_CANCELLED,
                WhatsAppOutboundAttempt.STATUS_FAILED,
                WhatsAppOutboundAttempt.STATUS_DEAD,
            ]
        ),
    )

    with Session(bind=db.engine, expire_on_commit=False) as session:
        quarantined = _quarantine_expired_outbound_sends(
            session,
            now=operation_now,
            tenant_id=resolved_tenant_id,
        )
        if quarantined:
            session.commit()
        due = and_(
            WhatsAppOutboundAttempt.status.in_(
                [
                    WhatsAppOutboundAttempt.STATUS_PENDING,
                    WhatsAppOutboundAttempt.STATUS_RETRY_WAIT,
                ]
            ),
            WhatsAppOutboundAttempt.attempt_count
            < WhatsAppOutboundAttempt.max_attempts,
            WhatsAppOutboundAttempt.available_at <= operation_now,
        )
        query = select(WhatsAppOutboundAttempt.id).where(due, no_blocking_predecessor)
        if resolved_tenant_id is not None:
            query = query.where(WhatsAppOutboundAttempt.tenant_id == resolved_tenant_id)
        candidate_ids = list(
            session.scalars(
                query.order_by(
                    WhatsAppOutboundAttempt.created_at.asc(),
                    WhatsAppOutboundAttempt.inbound_turn_id.asc(),
                    WhatsAppOutboundAttempt.sequence_no.asc(),
                ).limit(MAX_CANDIDATE_LIMIT)
            )
        )
        for candidate_id in candidate_ids:
            token = secrets.token_hex(24)
            leased_until = operation_now + timedelta(seconds=lease_duration)
            transition = session.execute(
                update(WhatsAppOutboundAttempt)
                .where(WhatsAppOutboundAttempt.id == int(candidate_id), due)
                .values(
                    status=WhatsAppOutboundAttempt.STATUS_SENDING,
                    attempt_count=WhatsAppOutboundAttempt.attempt_count + 1,
                    lease_token=token,
                    leased_until=leased_until,
                    completed_at=None,
                    last_error_code=None,
                    last_error_digest=None,
                    updated_at=operation_now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(transition.rowcount or 0) != 1:
                session.rollback()
                continue
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            attempt = session.get(
                WhatsAppOutboundAttempt,
                int(candidate_id),
                populate_existing=True,
            )
            if (
                attempt is None
                or attempt.status != WhatsAppOutboundAttempt.STATUS_SENDING
                or attempt.lease_token != token
            ):
                session.rollback()
                continue
            return OutboundAttemptClaim(
                database_id=int(attempt.id),
                attempt_id=str(attempt.attempt_id),
                tenant_id=int(attempt.tenant_id),
                inbound_turn_id=int(attempt.inbound_turn_id),
                provider_connection_id=attempt.provider_connection_id,
                provider_sender_id=attempt.provider_sender_id,
                provider=str(attempt.provider),
                stream_key=str(attempt.stream_key),
                sequence_no=int(attempt.sequence_no),
                message_kind=str(attempt.message_kind),
                idempotency_key=str(attempt.idempotency_key),
                payload=dict(attempt.payload_json or {}),
                payload_digest=str(attempt.payload_digest),
                attempt_count=int(attempt.attempt_count),
                max_attempts=int(attempt.max_attempts),
                lease_token=token,
                leased_until=_coerce_utc(attempt.leased_until),
            )
    return None


@outbox_persistence_operation
def accept_whatsapp_outbound_attempt(
    attempt_id: Any,
    lease_token: Any,
    provider_message_sid: Any,
    *,
    provider_status: Any = "queued",
    now: Optional[datetime] = None,
) -> bool:
    resolved_attempt_id = _normalize_identifier(
        attempt_id,
        code="invalid_attempt_id",
        max_length=36,
        lowercase=True,
    )
    resolved_token = _normalize_identifier(
        lease_token,
        code="invalid_lease_token",
        max_length=64,
        lowercase=True,
    )
    resolved_sid = _normalize_identifier(
        provider_message_sid,
        code="invalid_provider_message_sid",
        max_length=180,
    )
    status = str(provider_status or "").strip().lower()
    if status not in {
        "accepted",
        "scheduled",
        "queued",
        "sending",
        "sent",
        "delivered",
        "read",
    }:
        raise WhatsAppTurnValidationError("invalid_provider_status")
    operation_now = _coerce_utc(now)
    with Session(bind=db.engine, expire_on_commit=False) as session:
        transition = session.execute(
            update(WhatsAppOutboundAttempt)
            .where(
                WhatsAppOutboundAttempt.attempt_id == resolved_attempt_id,
                WhatsAppOutboundAttempt.status == WhatsAppOutboundAttempt.STATUS_SENDING,
                WhatsAppOutboundAttempt.lease_token == resolved_token,
            )
            .values(
                status=WhatsAppOutboundAttempt.STATUS_ACCEPTED,
                provider_message_sid=resolved_sid,
                provider_status=status,
                lease_token=None,
                leased_until=None,
                accepted_at=operation_now,
                completed_at=operation_now,
                last_error_code=None,
                last_error_digest=None,
                updated_at=operation_now,
            )
            .execution_options(synchronize_session=False)
        )
        if int(transition.rowcount or 0) != 1:
            session.rollback()
            return False
        session.commit()
        return True


_OUTBOUND_PROVIDER_PROGRESS = {
    "unknown": 0,
    "accepted": 10,
    "scheduled": 15,
    "queued": 20,
    "sending": 30,
    "sent": 40,
    "delivered": 50,
    "read": 60,
}
_OUTBOUND_PROVIDER_FAILURES = {"failed", "undelivered", "canceled", "cancelled"}


def _cancel_unsent_outbound_siblings(
    session: Session,
    attempt: WhatsAppOutboundAttempt,
    *,
    now: datetime,
    reason: Any,
) -> int:
    """Cancel later unsent messages from one failed turn, not future turns."""

    error_code, error_digest = _safe_error_details(reason)
    transition = session.execute(
        update(WhatsAppOutboundAttempt)
        .where(
            WhatsAppOutboundAttempt.tenant_id == attempt.tenant_id,
            WhatsAppOutboundAttempt.inbound_turn_id == attempt.inbound_turn_id,
            WhatsAppOutboundAttempt.sequence_no > attempt.sequence_no,
            WhatsAppOutboundAttempt.status.in_(
                [
                    WhatsAppOutboundAttempt.STATUS_PENDING,
                    WhatsAppOutboundAttempt.STATUS_RETRY_WAIT,
                ]
            ),
        )
        .values(
            status=WhatsAppOutboundAttempt.STATUS_CANCELLED,
            provider_status="cancelled",
            lease_token=None,
            leased_until=None,
            completed_at=now,
            last_error_code=error_code,
            last_error_digest=error_digest,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return int(transition.rowcount or 0)


def reconcile_whatsapp_outbound_status(
    *,
    tenant_id: Any,
    provider_message_sid: Any,
    provider_status: Any,
    attempt_id: Any = None,
    provider_sender_id: Any = None,
    error: Any = None,
    now: Optional[datetime] = None,
) -> bool:
    """Reconcile a signed provider callback, including a timed-out send.

    Twilio signs the complete callback URL, so the opaque ``attempt_id`` query
    parameter can safely correlate a callback that arrives after the API call
    timed out and the row was fenced as ``send_uncertain``.
    """

    resolved_tenant_id = _positive_int(tenant_id, code="invalid_tenant_id")
    resolved_sid = _normalize_identifier(
        provider_message_sid,
        code="invalid_provider_message_sid",
        max_length=180,
    )
    status = str(provider_status or "").strip().lower()
    if status not in set(_OUTBOUND_PROVIDER_PROGRESS).union(_OUTBOUND_PROVIDER_FAILURES):
        raise WhatsAppTurnValidationError("invalid_provider_status")
    resolved_attempt_id = (
        _normalize_identifier(
            attempt_id,
            code="invalid_attempt_id",
            max_length=36,
            lowercase=True,
        )
        if attempt_id not in (None, "")
        else None
    )
    resolved_sender_id = (
        _positive_int(provider_sender_id, code="invalid_provider_sender_id")
        if provider_sender_id is not None
        else None
    )
    operation_now = _coerce_utc(now)

    with Session(bind=db.engine, expire_on_commit=False) as session:
        query = select(WhatsAppOutboundAttempt).where(
            WhatsAppOutboundAttempt.tenant_id == resolved_tenant_id,
            WhatsAppOutboundAttempt.provider == "twilio",
        )
        if resolved_attempt_id:
            query = query.where(
                WhatsAppOutboundAttempt.attempt_id == resolved_attempt_id
            )
        else:
            query = query.where(
                WhatsAppOutboundAttempt.provider_message_sid == resolved_sid
            )
        if resolved_sender_id is not None:
            query = query.where(
                WhatsAppOutboundAttempt.provider_sender_id == resolved_sender_id
            )
        # Serialize callbacks for this row in PostgreSQL so a late "sent" or
        # "failed" callback cannot overwrite a newer "delivered/read" state.
        # SQLite ignores FOR UPDATE, while sequential monotonic guards still
        # cover the local test/runtime contract.
        attempt = session.scalar(query.with_for_update())
        if attempt is None:
            session.rollback()
            return False
        if attempt.provider_message_sid not in (None, "", resolved_sid):
            session.rollback()
            return False

        current_provider_status = str(attempt.provider_status or "unknown").lower()
        if current_provider_status in _OUTBOUND_PROVIDER_FAILURES:
            session.rollback()
            return current_provider_status == status
        if (
            status in _OUTBOUND_PROVIDER_PROGRESS
            and _OUTBOUND_PROVIDER_PROGRESS.get(status, 0)
            < _OUTBOUND_PROVIDER_PROGRESS.get(current_provider_status, 0)
        ):
            session.rollback()
            return True
        if (
            status in _OUTBOUND_PROVIDER_FAILURES
            and _OUTBOUND_PROVIDER_PROGRESS.get(current_provider_status, 0)
            >= _OUTBOUND_PROVIDER_PROGRESS["delivered"]
        ):
            session.rollback()
            return True

        values: dict[str, Any] = {
            "provider_message_sid": resolved_sid,
            "provider_status": status,
            "lease_token": None,
            "leased_until": None,
            "completed_at": operation_now,
            "updated_at": operation_now,
        }
        if status in _OUTBOUND_PROVIDER_FAILURES:
            error_code, error_digest = _safe_error_details(error or status)
            values.update(
                status=WhatsAppOutboundAttempt.STATUS_FAILED,
                last_error_code=error_code,
                last_error_digest=error_digest,
            )
        else:
            values.update(
                status=WhatsAppOutboundAttempt.STATUS_ACCEPTED,
                accepted_at=attempt.accepted_at or operation_now,
                last_error_code=None,
                last_error_digest=None,
            )
        session.execute(
            update(WhatsAppOutboundAttempt)
            .where(WhatsAppOutboundAttempt.id == attempt.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if status in _OUTBOUND_PROVIDER_FAILURES:
            _cancel_unsent_outbound_siblings(
                session,
                attempt,
                now=operation_now,
                reason="predecessor_provider_failed",
            )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
        return True


@outbox_persistence_operation
def _fail_whatsapp_outbound_attempt(
    attempt_id: Any,
    lease_token: Any,
    error: Any,
    *,
    permanent: bool,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Release a send that provably failed before provider I/O began."""

    resolved_attempt_id = _normalize_identifier(
        attempt_id,
        code="invalid_attempt_id",
        max_length=36,
        lowercase=True,
    )
    resolved_token = _normalize_identifier(
        lease_token,
        code="invalid_lease_token",
        max_length=64,
        lowercase=True,
    )
    error_code, error_digest = _safe_error_details(error)
    operation_now = _coerce_utc(now)
    with Session(bind=db.engine, expire_on_commit=False) as session:
        attempt = session.scalar(
            select(WhatsAppOutboundAttempt).where(
                WhatsAppOutboundAttempt.attempt_id == resolved_attempt_id,
                WhatsAppOutboundAttempt.status
                == WhatsAppOutboundAttempt.STATUS_SENDING,
                WhatsAppOutboundAttempt.lease_token == resolved_token,
            )
        )
        if attempt is None:
            session.rollback()
            return None
        dead = permanent or int(attempt.attempt_count) >= int(attempt.max_attempts)
        target = (
            WhatsAppOutboundAttempt.STATUS_DEAD
            if dead
            else WhatsAppOutboundAttempt.STATUS_RETRY_WAIT
        )
        available_at = (
            operation_now
            if dead
            else operation_now + timedelta(seconds=_retry_delay(attempt.attempt_count))
        )
        transition = session.execute(
            update(WhatsAppOutboundAttempt)
            .where(
                WhatsAppOutboundAttempt.id == attempt.id,
                WhatsAppOutboundAttempt.status
                == WhatsAppOutboundAttempt.STATUS_SENDING,
                WhatsAppOutboundAttempt.lease_token == resolved_token,
            )
            .values(
                status=target,
                available_at=available_at,
                lease_token=None,
                leased_until=None,
                completed_at=operation_now if dead else None,
                last_error_code=error_code,
                last_error_digest=error_digest,
                updated_at=operation_now,
            )
            .execution_options(synchronize_session=False)
        )
        if int(transition.rowcount or 0) != 1:
            session.rollback()
            return None
        if dead:
            _cancel_unsent_outbound_siblings(
                session,
                attempt,
                now=operation_now,
                reason="predecessor_dead_before_send",
            )
        session.commit()
        return target


def retry_whatsapp_outbound_attempt(
    attempt_id: Any,
    lease_token: Any,
    error: Any,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    return _fail_whatsapp_outbound_attempt(
        attempt_id,
        lease_token,
        error,
        permanent=False,
        now=now,
    )


def dead_whatsapp_outbound_attempt(
    attempt_id: Any,
    lease_token: Any,
    error: Any,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    return _fail_whatsapp_outbound_attempt(
        attempt_id,
        lease_token,
        error,
        permanent=True,
        now=now,
    )


@outbox_persistence_operation
def uncertain_whatsapp_outbound_attempt(
    attempt_id: Any,
    lease_token: Any,
    error: Any,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Fence a provider-bound failure as ambiguous; this state is never due."""

    resolved_attempt_id = _normalize_identifier(
        attempt_id,
        code="invalid_attempt_id",
        max_length=36,
        lowercase=True,
    )
    resolved_token = _normalize_identifier(
        lease_token,
        code="invalid_lease_token",
        max_length=64,
        lowercase=True,
    )
    error_code, error_digest = _safe_error_details(error)
    operation_now = _coerce_utc(now)
    with Session(bind=db.engine, expire_on_commit=False) as session:
        transition = session.execute(
            update(WhatsAppOutboundAttempt)
            .where(
                WhatsAppOutboundAttempt.attempt_id == resolved_attempt_id,
                WhatsAppOutboundAttempt.status == WhatsAppOutboundAttempt.STATUS_SENDING,
                WhatsAppOutboundAttempt.lease_token == resolved_token,
            )
            .values(
                status=WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN,
                lease_token=None,
                leased_until=None,
                completed_at=operation_now,
                last_error_code=error_code,
                last_error_digest=error_digest,
                updated_at=operation_now,
            )
            .execution_options(synchronize_session=False)
        )
        if int(transition.rowcount or 0) != 1:
            session.rollback()
            return False
        session.commit()
        return True


def summarize_whatsapp_turn_health(
    tenant_id: Any = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return tenant-safe queue health without payloads, addresses, or SIDs."""

    operation_now = _coerce_utc(now)
    resolved_tenant_id = (
        _positive_int(tenant_id, code="invalid_tenant_id")
        if tenant_id is not None
        else None
    )
    with Session(bind=db.engine, expire_on_commit=False) as session:
        inbound_query = select(
            WhatsAppInboundTurn.status,
            func.count(WhatsAppInboundTurn.id),
        )
        outbound_query = select(
            WhatsAppOutboundAttempt.status,
            func.count(WhatsAppOutboundAttempt.id),
        )
        if resolved_tenant_id is not None:
            inbound_query = inbound_query.where(
                WhatsAppInboundTurn.tenant_id == resolved_tenant_id
            )
            outbound_query = outbound_query.where(
                WhatsAppOutboundAttempt.tenant_id == resolved_tenant_id
            )
        inbound_rows = session.execute(
            inbound_query.group_by(WhatsAppInboundTurn.status)
        ).all()
        outbound_rows = session.execute(
            outbound_query.group_by(WhatsAppOutboundAttempt.status)
        ).all()
        inbound_by_status = {status: 0 for status in WhatsAppInboundTurn.STATUSES}
        outbound_by_status = {status: 0 for status in WhatsAppOutboundAttempt.STATUSES}
        for status, count in inbound_rows:
            inbound_by_status[str(status)] = int(count or 0)
        for status, count in outbound_rows:
            outbound_by_status[str(status)] = int(count or 0)

        inbound_due_query = select(func.count(WhatsAppInboundTurn.id)).where(
            _inbound_due_filter(operation_now)
        )
        outbound_due_query = select(func.count(WhatsAppOutboundAttempt.id)).where(
            WhatsAppOutboundAttempt.status.in_(
                [
                    WhatsAppOutboundAttempt.STATUS_PENDING,
                    WhatsAppOutboundAttempt.STATUS_RETRY_WAIT,
                ]
            ),
            WhatsAppOutboundAttempt.available_at <= operation_now,
        )
        stale_send_query = select(func.count(WhatsAppOutboundAttempt.id)).where(
            WhatsAppOutboundAttempt.status == WhatsAppOutboundAttempt.STATUS_SENDING,
            WhatsAppOutboundAttempt.leased_until.isnot(None),
            WhatsAppOutboundAttempt.leased_until <= operation_now,
        )
        if resolved_tenant_id is not None:
            inbound_due_query = inbound_due_query.where(
                WhatsAppInboundTurn.tenant_id == resolved_tenant_id
            )
            outbound_due_query = outbound_due_query.where(
                WhatsAppOutboundAttempt.tenant_id == resolved_tenant_id
            )
            stale_send_query = stale_send_query.where(
                WhatsAppOutboundAttempt.tenant_id == resolved_tenant_id
            )

        return {
            "contract_version": HEALTH_CONTRACT_VERSION,
            "tenant_id": resolved_tenant_id,
            "inbound": {
                "total": sum(inbound_by_status.values()),
                "due": int(session.scalar(inbound_due_query) or 0),
                "dead": inbound_by_status[WhatsAppInboundTurn.STATUS_DEAD],
                "in_flight": inbound_by_status[WhatsAppInboundTurn.STATUS_PROCESSING],
                "by_status": inbound_by_status,
            },
            "outbound": {
                "total": sum(outbound_by_status.values()),
                "due": int(session.scalar(outbound_due_query) or 0),
                "send_uncertain": outbound_by_status[
                    WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN
                ],
                "sending_stale": int(session.scalar(stale_send_query) or 0),
                "by_status": outbound_by_status,
            },
        }


__all__ = [
    "DEFAULT_DEAD_PAYLOAD_RETENTION_HOURS",
    "DEFAULT_PAYLOAD_SCRUB_BATCH_SIZE",
    "HEALTH_CONTRACT_VERSION",
    "INGEST_CREATED",
    "INGEST_DUPLICATE",
    "InboundTurnClaim",
    "InboundTurnCompletion",
    "InboundTurnReceipt",
    "OutboundAttemptClaim",
    "WhatsAppInboundDigestConflict",
    "WhatsAppTurnStateError",
    "WhatsAppTurnValidationError",
    "accept_whatsapp_outbound_attempt",
    "claim_next_whatsapp_inbound_turn",
    "claim_next_whatsapp_outbound_attempt",
    "complete_whatsapp_inbound_turn",
    "dead_whatsapp_outbound_attempt",
    "dead_whatsapp_inbound_turn",
    "derive_whatsapp_stream_key",
    "ingest_whatsapp_inbound_turn",
    "normalize_whatsapp_inbound_payload",
    "normalize_whatsapp_outbound_payload",
    "PAYLOAD_SCRUB_CONTRACT_VERSION",
    "PAYLOAD_SCRUB_RUN_CONTRACT_VERSION",
    "reconcile_whatsapp_outbound_status",
    "renew_whatsapp_inbound_turn_lease",
    "retry_whatsapp_outbound_attempt",
    "retry_whatsapp_inbound_turn",
    "scrub_expired_whatsapp_inbound_payloads",
    "summarize_whatsapp_turn_health",
    "uncertain_whatsapp_outbound_attempt",
]
