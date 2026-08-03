"""Security boundary for Chatboc's normalized omnichannel adapter contract.

The public catch-all endpoint must remain disabled.  Channel/provider adapters
may instead post a small, versioned JSON envelope to a concrete
``ProviderConnection``.  This module verifies request provenance before any
JSON is parsed, derives the tenant exclusively from that connection, and
normalizes only an allowlisted subset of the adapter payload.

The root HMAC secret is never shared with adapters.  A domain-separated key is
derived for each provider connection, which creates an explicit rotation and
revocation boundary per tenant/channel connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ProviderConnection, TenantProfile, WebhookDelivery, db
from services.webhook_delivery_service import (
    CLAIMED,
    DUPLICATE,
    IN_PROGRESS,
    WebhookDeliveryClaim,
)
from utils.validators import validate_email_address


CONTRACT_VERSION = "omnichannel.inbound.v1"
SIGNATURE_VERSION = "v1"
IDENTITY_VERSION = "omni-v1"

SIGNATURE_HEADER = "X-Chatboc-Signature-V1"
TIMESTAMP_HEADER = "X-Chatboc-Timestamp"
EVENT_ID_HEADER = "X-Chatboc-Event-Id"
EVENT_TYPE_HEADER = "X-Chatboc-Event-Type"

READY_CONNECTION_STATUSES = frozenset({"online", "approved", "connected", "active"})
ALLOWED_MESSAGE_KINDS = frozenset(
    {
        "text",
        "emoji",
        "reaction",
        "interactive",
        "audio",
        "image",
        "video",
        "document",
        "location",
        "call_event",
    }
)
ALLOWED_CALL_STATES = frozenset(
    {
        "initiated",
        "ringing",
        "answered",
        "in_progress",
        "completed",
        "busy",
        "failed",
        "no_answer",
        "canceled",
    }
)

_CHANNEL_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SIGNATURE_RE = re.compile(r"sha256=([0-9a-f]{64})")
_EVENT_TOKEN_RE = re.compile(r"[^\s\x00-\x1f\x7f]{1,255}")
_PHONE_RE = re.compile(r"\+?[0-9][0-9 ().-]{5,30}[0-9]")
_PRIVATE_MEDIA_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost")
_ADAPTER_DELIVERY_STALE_AFTER = timedelta(minutes=5)


class OmnichannelAdapterError(ValueError):
    """A safe, public error emitted by the signed adapter boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class VerifiedAdapterRequest:
    connection: ProviderConnection
    tenant: TenantProfile
    provider_namespace: str
    event_id: str
    event_type: str
    timestamp: int
    payload_digest: str
    connection_secret: bytes

    @property
    def delivery_event_id(self) -> str:
        """Return the opaque, connection-scoped event identity persisted locally."""

        digest = hmac.new(
            self.connection_secret,
            (
                b"chatboc.omnichannel.delivery-event.v1\0"
                + str(self.event_id).encode("utf-8")
            ),
            hashlib.sha256,
        ).hexdigest()
        return f"omni-event-v1:{digest}"


@dataclass(frozen=True)
class NormalizedAdapterEvent:
    ticket_payload: dict[str, Any]
    message_kind: str
    has_media: bool
    awaiting_media_processing: bool


def _config_value(config: Mapping[str, Any], key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def signed_inbound_enabled(config: Mapping[str, Any]) -> bool:
    return str(
        _config_value(config, "OMNICHANNEL_SIGNED_INBOUND_MODE", "disabled")
        or "disabled"
    ).strip().lower() == "enforce"


def _root_secret(config: Mapping[str, Any]) -> bytes:
    raw = str(
        _config_value(config, "OMNICHANNEL_INBOUND_HMAC_SECRET_V1", "") or ""
    ).encode("utf-8")
    if len(raw) < 32:
        raise OmnichannelAdapterError(
            "omnichannel_adapter_not_configured",
            "El adaptador omnicanal no está configurado.",
            status_code=503,
            retryable=False,
        )
    return raw


def _connection_key_material(connection: ProviderConnection) -> bytes:
    return (
        f"{SIGNATURE_VERSION}\0{int(connection.id)}\0"
        f"{str(connection.tenant_id)}\0{str(connection.provider or '').strip().lower()}\0"
        f"{str(connection.channel or '').strip().lower()}"
    ).encode("utf-8")


def derive_connection_signing_secret(
    root_secret: str | bytes,
    connection: ProviderConnection,
) -> bytes:
    """Derive the per-connection secret issued to a trusted adapter."""

    root = root_secret.encode("utf-8") if isinstance(root_secret, str) else bytes(root_secret)
    if len(root) < 32:
        raise ValueError("root_secret must contain at least 32 bytes")
    return hmac.new(
        root,
        b"chatboc.omnichannel.connection-key.v1\0" + _connection_key_material(connection),
        hashlib.sha256,
    ).digest()


def _signature_material(
    *,
    timestamp: int,
    connection_id: int,
    event_id: str,
    event_type: str,
    payload_digest: str,
) -> bytes:
    return (
        f"{SIGNATURE_VERSION}\n{timestamp}\n{connection_id}\n"
        f"{event_id}\n{event_type}\n{payload_digest}"
    ).encode("utf-8")


def build_adapter_signature(
    connection_secret: bytes,
    *,
    timestamp: int,
    connection_id: int,
    event_id: str,
    event_type: str,
    raw_body: bytes,
) -> str:
    """Return the exact signature value required by the adapter endpoint."""

    payload_digest = hashlib.sha256(raw_body).hexdigest()
    digest = hmac.new(
        bytes(connection_secret),
        _signature_material(
            timestamp=timestamp,
            connection_id=connection_id,
            event_id=event_id,
            event_type=event_type,
            payload_digest=payload_digest,
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def _header(headers: Mapping[str, Any], name: str) -> str:
    value = str(headers.get(name, "") or "").strip()
    if not value:
        raise OmnichannelAdapterError(
            "missing_adapter_signature_headers",
            "Faltan cabeceras obligatorias del adaptador.",
            status_code=401,
        )
    return value


def _event_token(value: str, *, field: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > max_length or not _EVENT_TOKEN_RE.fullmatch(normalized):
        raise OmnichannelAdapterError(
            f"invalid_{field}",
            "La identidad del evento no es válida.",
            status_code=400,
        )
    return normalized


def adapter_payload_limit(config: Mapping[str, Any]) -> int:
    """Return the bounded per-request byte limit used before reading the body."""

    try:
        value = int(_config_value(config, "OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES", 65536))
    except (TypeError, ValueError, OverflowError):
        value = 65536
    return max(1024, min(value, 262144))


def _max_clock_skew(config: Mapping[str, Any]) -> int:
    try:
        value = int(
            _config_value(config, "OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS", 300)
        )
    except (TypeError, ValueError, OverflowError):
        value = 300
    return max(30, min(value, 900))


def verify_adapter_request(
    *,
    config: Mapping[str, Any],
    connection_id: int,
    headers: Mapping[str, Any],
    raw_body: bytes,
    now: datetime | None = None,
) -> VerifiedAdapterRequest:
    """Authenticate a request and derive its tenant before payload parsing."""

    if not signed_inbound_enabled(config):
        raise OmnichannelAdapterError(
            "signed_omnichannel_inbound_disabled",
            "El adaptador omnicanal firmado no está habilitado.",
            status_code=404,
        )

    if not isinstance(raw_body, bytes) or not raw_body:
        raise OmnichannelAdapterError(
            "empty_adapter_payload",
            "El cuerpo del evento está vacío.",
            status_code=400,
        )
    if len(raw_body) > adapter_payload_limit(config):
        raise OmnichannelAdapterError(
            "adapter_payload_too_large",
            "El evento supera el tamaño permitido.",
            status_code=413,
        )

    try:
        normalized_connection_id = int(connection_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OmnichannelAdapterError(
            "adapter_connection_not_found",
            "No existe una conexión activa para este adaptador.",
            status_code=404,
        ) from exc
    if isinstance(connection_id, bool) or normalized_connection_id <= 0:
        raise OmnichannelAdapterError(
            "adapter_connection_not_found",
            "No existe una conexión activa para este adaptador.",
            status_code=404,
        )

    connection = db.session.get(ProviderConnection, normalized_connection_id)
    tenant = getattr(connection, "tenant", None) if connection is not None else None
    try:
        connection_tenant_id = int(getattr(connection, "tenant_id", 0) or 0)
        relation_tenant_id = int(getattr(tenant, "id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        connection_tenant_id = 0
        relation_tenant_id = -1
    if (
        connection is None
        or tenant is None
        or connection_tenant_id <= 0
        or relation_tenant_id != connection_tenant_id
        or not bool(getattr(tenant, "is_active", False))
        or str(getattr(connection, "status", "") or "").strip().lower()
        not in READY_CONNECTION_STATUSES
    ):
        raise OmnichannelAdapterError(
            "adapter_connection_not_found",
            "No existe una conexión activa para este adaptador.",
            status_code=404,
        )

    event_id = _event_token(
        _header(headers, EVENT_ID_HEADER),
        field="adapter_event_id",
        max_length=255,
    )
    event_type = _event_token(
        _header(headers, EVENT_TYPE_HEADER),
        field="adapter_event_type",
        max_length=128,
    )
    timestamp_raw = _header(headers, TIMESTAMP_HEADER)
    try:
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError) as exc:
        raise OmnichannelAdapterError(
            "invalid_adapter_timestamp",
            "La fecha del evento no es válida.",
            status_code=401,
        ) from exc

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference_timestamp = int(reference.timestamp())
    if abs(reference_timestamp - timestamp) > _max_clock_skew(config):
        raise OmnichannelAdapterError(
            "stale_adapter_request",
            "La firma del evento está vencida.",
            status_code=401,
        )

    signature = _header(headers, SIGNATURE_HEADER).lower()
    signature_match = _SIGNATURE_RE.fullmatch(signature)
    if signature_match is None:
        raise OmnichannelAdapterError(
            "invalid_adapter_signature",
            "La firma del evento no es válida.",
            status_code=401,
        )

    root = _root_secret(config)
    connection_secret = derive_connection_signing_secret(root, connection)
    payload_digest = hashlib.sha256(raw_body).hexdigest()
    expected = build_adapter_signature(
        connection_secret,
        timestamp=timestamp,
        connection_id=connection.id,
        event_id=event_id,
        event_type=event_type,
        raw_body=raw_body,
    )
    if not hmac.compare_digest(signature, expected):
        raise OmnichannelAdapterError(
            "invalid_adapter_signature",
            "La firma del evento no es válida.",
            status_code=401,
        )

    provider = str(connection.provider or "adapter").strip().lower() or "adapter"
    return VerifiedAdapterRequest(
        connection=connection,
        tenant=tenant,
        provider_namespace=f"omnichannel:{connection.id}:{provider}"[:64],
        event_id=event_id,
        event_type=event_type,
        timestamp=timestamp,
        payload_digest=payload_digest,
        connection_secret=connection_secret,
    )


def _delivery_claim(
    delivery: WebhookDelivery,
    outcome: str,
) -> WebhookDeliveryClaim:
    return WebhookDeliveryClaim(
        outcome=outcome,
        delivery_id=int(delivery.id),
        provider=str(delivery.provider),
        event_id=str(delivery.event_id),
        event_type=str(delivery.event_type),
        payload_digest=str(delivery.payload_digest),
        attempts=int(delivery.attempts),
    )


def _utc_datetime(value: datetime | None) -> datetime:
    normalized = value or datetime.now(timezone.utc)
    if not isinstance(normalized, datetime):
        raise OmnichannelAdapterError(
            "invalid_adapter_delivery_clock",
            "No pudimos validar el reloj del evento.",
            status_code=503,
            retryable=True,
        )
    if normalized.tzinfo is None:
        return normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc)


def claim_verified_adapter_delivery(
    verified: VerifiedAdapterRequest,
    raw_body: bytes,
    *,
    stale_after: timedelta = _ADAPTER_DELIVERY_STALE_AFTER,
    now: datetime | None = None,
) -> WebhookDeliveryClaim:
    """Atomically claim one signed event without ever rebinding its identity.

    The generic webhook claimer intentionally permits a failed delivery to be
    retried with refreshed metadata.  A signed adapter event has a stronger
    contract: ``(provider namespace, opaque event id)`` is permanently bound
    to its original event type and body digest.  The compare-and-swap update
    below keeps that invariant even when conflicting retries race each other.
    """

    if not isinstance(raw_body, bytes):
        raise OmnichannelAdapterError(
            "invalid_adapter_payload",
            "El cuerpo del evento no es válido.",
            status_code=400,
        )
    body_digest = hashlib.sha256(raw_body).hexdigest()
    if not hmac.compare_digest(body_digest, verified.payload_digest):
        raise OmnichannelAdapterError(
            "adapter_event_payload_conflict",
            "El contenido no coincide con la solicitud verificada.",
            status_code=409,
            retryable=False,
        )
    if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
        raise OmnichannelAdapterError(
            "invalid_adapter_delivery_window",
            "La ventana de reintento no es válida.",
            status_code=503,
            retryable=True,
        )

    current_time = _utc_datetime(now)
    stale_before = current_time - stale_after

    # A unique insert can race, and a stale/failed claim can change state
    # between SELECT and UPDATE.  Bound retries plus a CAS fence cover both
    # windows without mutating event_type or payload_digest.
    for _attempt in range(5):
        with Session(bind=db.engine, expire_on_commit=False) as session:
            delivery = session.scalar(
                select(WebhookDelivery).where(
                    WebhookDelivery.provider == verified.provider_namespace,
                    WebhookDelivery.event_id == verified.delivery_event_id,
                )
            )
            if delivery is None:
                delivery = WebhookDelivery(
                    provider=verified.provider_namespace,
                    event_id=verified.delivery_event_id,
                    event_type=verified.event_type,
                    payload_digest=verified.payload_digest,
                    status=WebhookDelivery.STATUS_PROCESSING,
                    attempts=1,
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(delivery)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    continue
                return _delivery_claim(delivery, CLAIMED)

            stored_digest = str(delivery.payload_digest or "")
            stored_event_type = str(delivery.event_type or "")
            if not hmac.compare_digest(
                stored_digest,
                verified.payload_digest,
            ) or not hmac.compare_digest(stored_event_type, verified.event_type):
                session.rollback()
                raise OmnichannelAdapterError(
                    "adapter_event_payload_conflict",
                    "El identificador del evento ya está asociado a otro contenido.",
                    status_code=409,
                    retryable=False,
                )

            if delivery.status == WebhookDelivery.STATUS_PROCESSED:
                result = _delivery_claim(delivery, DUPLICATE)
                session.rollback()
                return result

            updated_at = _utc_datetime(delivery.updated_at)
            if (
                delivery.status == WebhookDelivery.STATUS_PROCESSING
                and updated_at > stale_before
            ):
                result = _delivery_claim(delivery, IN_PROGRESS)
                session.rollback()
                return result
            if delivery.status not in {
                WebhookDelivery.STATUS_PROCESSING,
                WebhookDelivery.STATUS_FAILED,
            }:
                session.rollback()
                raise OmnichannelAdapterError(
                    "invalid_adapter_delivery_state",
                    "El estado persistido del evento no es válido.",
                    status_code=503,
                    retryable=True,
                )

            current_attempt = int(delivery.attempts or 0)
            if current_attempt < 1:
                session.rollback()
                raise OmnichannelAdapterError(
                    "invalid_adapter_delivery_state",
                    "El estado persistido del evento no es válido.",
                    status_code=503,
                    retryable=True,
                )
            transition = session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery.id,
                    WebhookDelivery.provider == verified.provider_namespace,
                    WebhookDelivery.event_id == verified.delivery_event_id,
                    WebhookDelivery.event_type == verified.event_type,
                    WebhookDelivery.payload_digest == verified.payload_digest,
                    WebhookDelivery.attempts == current_attempt,
                    or_(
                        WebhookDelivery.status == WebhookDelivery.STATUS_FAILED,
                        and_(
                            WebhookDelivery.status
                            == WebhookDelivery.STATUS_PROCESSING,
                            WebhookDelivery.updated_at <= stale_before,
                        ),
                    ),
                )
                .values(
                    status=WebhookDelivery.STATUS_PROCESSING,
                    attempts=current_attempt + 1,
                    processed_at=None,
                    last_error=None,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if transition.rowcount == 1:
                delivery_id = int(delivery.id)
                session.commit()
                return WebhookDeliveryClaim(
                    outcome=CLAIMED,
                    delivery_id=delivery_id,
                    provider=verified.provider_namespace,
                    event_id=verified.delivery_event_id,
                    event_type=verified.event_type,
                    payload_digest=verified.payload_digest,
                    attempts=current_attempt + 1,
                )
            session.rollback()

    raise OmnichannelAdapterError(
        "adapter_delivery_claim_contention",
        "No pudimos reclamar el evento de forma segura.",
        status_code=503,
        retryable=True,
    )


def parse_adapter_json(raw_body: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmnichannelAdapterError(
            "invalid_adapter_json",
            "El evento debe ser JSON UTF-8 válido.",
            status_code=400,
        ) from exc
    if not isinstance(decoded, dict):
        raise OmnichannelAdapterError(
            "invalid_adapter_payload",
            "El evento debe ser un objeto JSON.",
            status_code=400,
        )
    return decoded


def _clean_text(value: Any, *, max_length: int) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized[:max_length]


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    error_code: str,
    allow_line_breaks: bool = False,
) -> str | None:
    """Validate one JSON string without silently changing its identity/content."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise OmnichannelAdapterError(
            error_code,
            f"El campo {field} no es válido.",
            status_code=422,
        )
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise OmnichannelAdapterError(
            error_code,
            f"El campo {field} supera el tamaño permitido.",
            status_code=422,
        )
    permitted_controls = {"\r", "\n", "\t"} if allow_line_breaks else set()
    if any(
        (ord(character) < 32 or ord(character) == 127)
        and character not in permitted_controls
        for character in normalized
    ):
        raise OmnichannelAdapterError(
            error_code,
            f"El campo {field} contiene caracteres no permitidos.",
            status_code=422,
        )
    return normalized


def _contact_external_id(value: Any) -> str:
    normalized = _bounded_text(
        value,
        field="contact.external_id",
        max_length=255,
        error_code="invalid_adapter_contact_id",
    )
    if not normalized or any(character.isspace() for character in normalized):
        raise OmnichannelAdapterError(
            "invalid_adapter_contact_id",
            "La identidad externa del contacto no es válida.",
            status_code=422,
        )
    return normalized


def _contact_email(value: Any) -> str | None:
    normalized = _bounded_text(
        value,
        field="contact.email",
        max_length=254,
        error_code="invalid_adapter_contact_email",
    )
    if normalized is None:
        return None
    normalized = normalized.lower()
    if not validate_email_address(normalized):
        raise OmnichannelAdapterError(
            "invalid_adapter_contact_email",
            "El email del contacto no es válido.",
            status_code=422,
        )
    return normalized


def _contact_phone(value: Any) -> str | None:
    normalized = _bounded_text(
        value,
        field="contact.phone",
        max_length=32,
        error_code="invalid_adapter_contact_phone",
    )
    if normalized is None:
        return None
    if not _PHONE_RE.fullmatch(normalized):
        raise OmnichannelAdapterError(
            "invalid_adapter_contact_phone",
            "El teléfono del contacto no es válido.",
            status_code=422,
        )
    return normalized


def _unsafe_media_hostname(hostname: str) -> bool:
    normalized = str(hostname or "").strip().lower().rstrip(".")
    if not normalized or normalized == "localhost" or normalized.endswith(
        _PRIVATE_MEDIA_HOST_SUFFIXES
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


def _validate_media_kind(kind: str, media: list[dict[str, Any]]) -> None:
    expected_prefixes = {
        "audio": ("audio/",),
        "image": ("image/",),
        "video": ("video/",),
        "document": ("application/", "text/"),
    }
    expected = expected_prefixes.get(kind)
    if expected is None:
        return
    for item in media:
        mime_type = item.get("mime_type")
        if mime_type and not str(mime_type).startswith(expected):
            raise OmnichannelAdapterError(
                "adapter_media_kind_mismatch",
                "El tipo de archivo no coincide con el mensaje.",
                status_code=422,
            )


def _coordinate(value: Any, *, field: str, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise OmnichannelAdapterError(
            "invalid_adapter_location",
            f"La coordenada {field} no es válida.",
            status_code=422,
        )
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise OmnichannelAdapterError(
            "invalid_adapter_location",
            f"La coordenada {field} no es válida.",
            status_code=422,
        ) from exc
    if not math.isfinite(normalized) or normalized < minimum or normalized > maximum:
        raise OmnichannelAdapterError(
            "invalid_adapter_location",
            f"La coordenada {field} no es válida.",
            status_code=422,
        )
    return normalized


def _positive_owner_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized > 0 else None


def _normalize_media(raw_media: Any) -> list[dict[str, Any]]:
    if raw_media in (None, []):
        return []
    if not isinstance(raw_media, list) or len(raw_media) > 8:
        raise OmnichannelAdapterError(
            "invalid_adapter_media",
            "La lista de archivos no es válida.",
            status_code=422,
        )

    normalized: list[dict[str, Any]] = []
    for raw in raw_media:
        if not isinstance(raw, dict):
            raise OmnichannelAdapterError(
                "invalid_adapter_media",
                "La lista de archivos no es válida.",
                status_code=422,
            )
        media: dict[str, Any] = {}
        provider_media_id = _bounded_text(
            raw.get("provider_media_id"),
            field="message.media.provider_media_id",
            max_length=255,
            error_code="invalid_adapter_media",
        )
        url = _bounded_text(
            raw.get("url"),
            field="message.media.url",
            max_length=2048,
            error_code="invalid_adapter_media_url",
        )
        if url:
            parsed = urlsplit(url)
            try:
                parsed.port
            except ValueError as exc:
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_url",
                    "La referencia del archivo no es segura.",
                    status_code=422,
                ) from exc
            if (
                parsed.scheme.lower() != "https"
                or not parsed.netloc
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or any(character.isspace() for character in url)
                or _unsafe_media_hostname(parsed.hostname)
            ):
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_url",
                    "La referencia del archivo no es segura.",
                    status_code=422,
                )
            media["url"] = url
        if provider_media_id:
            media["provider_media_id"] = provider_media_id
        if not media:
            raise OmnichannelAdapterError(
                "missing_adapter_media_reference",
                "Falta la referencia del archivo.",
                status_code=422,
            )

        mime_type = _bounded_text(
            raw.get("mime_type"),
            field="message.media.mime_type",
            max_length=120,
            error_code="invalid_adapter_media_mime",
        )
        if mime_type:
            mime_normalized = mime_type.lower()
            if "/" not in mime_normalized or any(ch.isspace() for ch in mime_normalized):
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_mime",
                    "El tipo de archivo no es válido.",
                    status_code=422,
                )
            media["mime_type"] = mime_normalized
        digest = _bounded_text(
            raw.get("sha256"),
            field="message.media.sha256",
            max_length=64,
            error_code="invalid_adapter_media_digest",
        )
        if digest:
            digest = digest.lower()
            if not _SHA256_RE.fullmatch(digest):
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_digest",
                    "La huella del archivo no es válida.",
                    status_code=422,
                )
            media["sha256"] = digest
        size = raw.get("size_bytes")
        if size is not None:
            if isinstance(size, bool) or isinstance(size, float):
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_size",
                    "El tamaño del archivo no es válido.",
                    status_code=422,
                )
            try:
                size_value = int(size)
            except (TypeError, ValueError) as exc:
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_size",
                    "El tamaño del archivo no es válido.",
                    status_code=422,
                ) from exc
            if size_value <= 0 or size_value > 25 * 1024 * 1024:
                raise OmnichannelAdapterError(
                    "invalid_adapter_media_size",
                    "El tamaño del archivo no es válido.",
                    status_code=422,
                )
            media["size_bytes"] = size_value
        normalized.append(media)
    return normalized


def _scoped_external_identity(
    verified: VerifiedAdapterRequest,
    raw_external_id: str,
) -> str:
    material = (
        f"{IDENTITY_VERSION}\0{verified.connection.id}\0"
        f"{verified.connection.channel}\0{raw_external_id}"
    ).encode("utf-8")
    digest = hmac.new(
        verified.connection_secret,
        b"chatboc.omnichannel.contact.v1\0" + material,
        hashlib.sha256,
    ).hexdigest()
    return f"{IDENTITY_VERSION}:{digest}"


def normalize_adapter_event(
    payload: Mapping[str, Any],
    verified: VerifiedAdapterRequest,
) -> NormalizedAdapterEvent:
    """Normalize a verified envelope into the existing ticket ingestion API."""

    if str(payload.get("contract_version") or "").strip() != CONTRACT_VERSION:
        raise OmnichannelAdapterError(
            "unsupported_adapter_contract",
            "La versión del contrato no es compatible.",
            status_code=422,
        )
    if str(payload.get("direction") or "").strip().lower() != "inbound":
        raise OmnichannelAdapterError(
            "invalid_adapter_direction",
            "Solo se aceptan eventos entrantes.",
            status_code=422,
        )

    channel = str(payload.get("channel") or "").strip().lower()
    expected_channel = str(verified.connection.channel or "").strip().lower()
    if not _CHANNEL_RE.fullmatch(channel) or not hmac.compare_digest(channel, expected_channel):
        raise OmnichannelAdapterError(
            "adapter_channel_mismatch",
            "El canal no coincide con la conexión verificada.",
            status_code=422,
        )

    payload_event_type = _bounded_text(
        payload.get("event_type"),
        field="event_type",
        max_length=128,
        error_code="invalid_adapter_event_type",
    )
    if payload_event_type and not hmac.compare_digest(payload_event_type, verified.event_type):
        raise OmnichannelAdapterError(
            "adapter_event_type_mismatch",
            "El tipo de evento no coincide con la firma.",
            status_code=409,
        )

    contact = payload.get("contact")
    if not isinstance(contact, dict):
        raise OmnichannelAdapterError(
            "missing_adapter_contact",
            "Falta la identidad del contacto.",
            status_code=422,
        )
    if contact.get("external_id") in (None, ""):
        raise OmnichannelAdapterError(
            "missing_adapter_contact_id",
            "Falta la identidad externa del contacto.",
            status_code=422,
        )
    raw_external_id = _contact_external_id(contact.get("external_id"))

    message = payload.get("message")
    if not isinstance(message, dict):
        raise OmnichannelAdapterError(
            "missing_adapter_message",
            "Falta el contenido del mensaje.",
            status_code=422,
        )
    kind = str(message.get("kind") or "").strip().lower()
    if kind not in ALLOWED_MESSAGE_KINDS:
        raise OmnichannelAdapterError(
            "unsupported_adapter_message_kind",
            "El tipo de mensaje no está permitido.",
            status_code=422,
        )

    media = _normalize_media(message.get("media"))
    _validate_media_kind(kind, media)
    text = _bounded_text(
        message.get("text"),
        field="message.text",
        max_length=16000,
        error_code="invalid_adapter_message_text",
        allow_line_breaks=True,
    )
    transcript = _bounded_text(
        message.get("transcript"),
        field="message.transcript",
        max_length=16000,
        error_code="invalid_adapter_transcript",
        allow_line_breaks=True,
    )
    caption = _bounded_text(
        message.get("caption"),
        field="message.caption",
        max_length=4000,
        error_code="invalid_adapter_caption",
        allow_line_breaks=True,
    )
    raw_location = message.get("location")
    if raw_location is not None and not isinstance(raw_location, dict):
        raise OmnichannelAdapterError(
            "invalid_adapter_location",
            "La ubicación no es válida.",
            status_code=422,
        )
    location = raw_location or {}
    raw_call = message.get("call")
    if raw_call is not None and not isinstance(raw_call, dict):
        raise OmnichannelAdapterError(
            "invalid_adapter_call",
            "Los datos de la llamada no son válidos.",
            status_code=422,
        )
    if kind != "call_event" and "call" in message:
        raise OmnichannelAdapterError(
            "unexpected_adapter_call",
            "Los datos de llamada solo se aceptan en call_event.",
            status_code=422,
        )
    call = raw_call or {}

    if kind == "call_event":
        call_state = str(call.get("state") or "").strip().lower()
        if call_state not in ALLOWED_CALL_STATES:
            raise OmnichannelAdapterError(
                "invalid_adapter_call_state",
                "El estado de la llamada no es válido.",
                status_code=422,
            )
        provider_call_id = _bounded_text(
            call.get("provider_call_id"),
            field="message.call.provider_call_id",
            max_length=255,
            error_code="invalid_adapter_call_id",
        )
        if not provider_call_id or any(
            character.isspace() for character in provider_call_id
        ):
            raise OmnichannelAdapterError(
                "missing_adapter_call_id",
                "Falta la identidad externa de la llamada.",
                status_code=422,
            )
    else:
        call_state = ""
        provider_call_id = None

    latitude = _coordinate(
        location.get("lat"),
        field="latitud",
        minimum=-90.0,
        maximum=90.0,
    )
    longitude = _coordinate(
        location.get("lng", location.get("lon")),
        field="longitud",
        minimum=-180.0,
        maximum=180.0,
    )
    if (latitude is None) != (longitude is None):
        raise OmnichannelAdapterError(
            "invalid_adapter_location",
            "La ubicación requiere latitud y longitud juntas.",
            status_code=422,
        )
    address = _bounded_text(
        location.get("address"),
        field="message.location.address",
        max_length=500,
        error_code="invalid_adapter_location",
    )

    if kind in {"text", "emoji", "reaction", "interactive"} and not text:
        raise OmnichannelAdapterError(
            "missing_adapter_message_text",
            "El mensaje no contiene texto.",
            status_code=422,
        )
    if kind in {"audio", "image", "video", "document"} and not media:
        raise OmnichannelAdapterError(
            "missing_adapter_media",
            "El mensaje no contiene un archivo verificable.",
            status_code=422,
        )
    if kind == "location" and latitude is None and longitude is None and not address:
        raise OmnichannelAdapterError(
            "missing_adapter_location",
            "El mensaje no contiene una ubicación.",
            status_code=422,
        )

    if text:
        effective_message = text
    elif transcript:
        effective_message = transcript
    elif caption:
        effective_message = caption
    elif kind == "location":
        effective_message = address or "Ubicación compartida por el contacto."
    elif kind == "call_event":
        effective_message = f"Evento de llamada: {call_state}."
    else:
        labels = {
            "audio": "Audio recibido; transcripción pendiente.",
            "image": "Imagen recibida; análisis pendiente.",
            "video": "Video recibido; análisis pendiente.",
            "document": "Documento recibido; análisis pendiente.",
        }
        effective_message = labels[kind]

    tenant = verified.tenant
    raw_municipio_id = getattr(tenant, "municipio_id", None)
    raw_pyme_id = getattr(tenant, "pyme_id", None)
    municipio_id = _positive_owner_id(raw_municipio_id)
    pyme_id = _positive_owner_id(raw_pyme_id)
    has_municipio = municipio_id is not None
    has_pyme = pyme_id is not None
    if has_municipio == has_pyme:
        raise OmnichannelAdapterError(
            "adapter_tenant_owner_invalid",
            "La organización no tiene un propietario inequívoco.",
            status_code=409,
        )
    if (
        (raw_municipio_id is not None and municipio_id is None)
        or (raw_pyme_id is not None and pyme_id is None)
    ):
        raise OmnichannelAdapterError(
            "adapter_tenant_owner_invalid",
            "La organización no tiene un propietario inequívoco.",
            status_code=409,
        )
    if has_municipio:
        ticket_type = "municipio"
    else:
        ticket_type = "pyme"

    scoped_contact = {
        "external_id": _scoped_external_identity(verified, raw_external_id),
        "nombre": _bounded_text(
            contact.get("name"),
            field="contact.name",
            max_length=100,
            error_code="invalid_adapter_contact_name",
        )
        or "Contacto omnicanal",
        # These fields remain tenant-ticket metadata.  The ingestion service's
        # scoped identity mode never uses them for global User lookup/linking.
        "email": _contact_email(contact.get("email")),
        "telefono": _contact_phone(contact.get("phone")),
    }
    scoped_contact = {key: value for key, value in scoped_contact.items() if value}

    ticket_payload: dict[str, Any] = {
        "tipo_ticket": ticket_type,
        "tenant_id": tenant.id,
        "municipio_id": municipio_id,
        "pyme_id": pyme_id,
        "canal": channel,
        "contacto": scoped_contact,
        "mensaje": effective_message,
        "asunto": _bounded_text(
            payload.get("subject"),
            field="subject",
            max_length=255,
            error_code="invalid_adapter_subject",
        )
        or f"Interacción {channel}",
        "categoria": _bounded_text(
            payload.get("category"),
            field="category",
            max_length=100,
            error_code="invalid_adapter_category",
        ),
        "source": f"signed_adapter:{verified.connection.provider}",
        "source_event_id": verified.delivery_event_id,
        "source_event_type": verified.event_type,
        "provider_connection_id": verified.connection.id,
        "idempotency_key": (
            f"omnichannel:{tenant.id}:{verified.connection.id}:"
            + hashlib.sha256(verified.delivery_event_id.encode("utf-8")).hexdigest()
        ),
        "identity_contract": "tenant_provider_scoped_v1",
        "message_kind": kind,
        "attachments": media,
        "adapter_contract": {
            "version": CONTRACT_VERSION,
            "signature_version": SIGNATURE_VERSION,
            "provider": str(verified.connection.provider or "").strip().lower(),
            "connection_id": verified.connection.id,
            "event_type": verified.event_type,
            "payload_sha256": verified.payload_digest,
        },
    }
    if latitude is not None:
        ticket_payload["lat"] = latitude
    if longitude is not None:
        ticket_payload["lng"] = longitude
    if address:
        ticket_payload["address"] = address
    if call:
        duration = call.get("duration_seconds")
        if duration is not None:
            if isinstance(duration, (bool, float)):
                raise OmnichannelAdapterError(
                    "invalid_adapter_call_duration",
                    "La duración de la llamada no es válida.",
                    status_code=422,
                )
            try:
                duration = int(duration)
            except (TypeError, ValueError) as exc:
                raise OmnichannelAdapterError(
                    "invalid_adapter_call_duration",
                    "La duración de la llamada no es válida.",
                    status_code=422,
                ) from exc
            if duration < 0 or duration > 24 * 60 * 60:
                raise OmnichannelAdapterError(
                    "invalid_adapter_call_duration",
                    "La duración de la llamada no es válida.",
                    status_code=422,
                )
        ticket_payload["call_event"] = {
            "state": call_state,
            "duration_seconds": duration,
            "provider_call_id_sha256": (
                hashlib.sha256(
                    provider_call_id.encode("utf-8")
                ).hexdigest()
                if provider_call_id
                else None
            ),
        }

    awaiting_media_processing = bool(
        kind in {"audio", "image", "video", "document"}
        and not (transcript or text or caption)
    )
    return NormalizedAdapterEvent(
        ticket_payload=ticket_payload,
        message_kind=kind,
        has_media=bool(media),
        awaiting_media_processing=awaiting_media_processing,
    )


__all__ = [
    "CONTRACT_VERSION",
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "OmnichannelAdapterError",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "NormalizedAdapterEvent",
    "VerifiedAdapterRequest",
    "adapter_payload_limit",
    "build_adapter_signature",
    "claim_verified_adapter_delivery",
    "derive_connection_signing_secret",
    "normalize_adapter_event",
    "parse_adapter_json",
    "signed_inbound_enabled",
    "verify_adapter_request",
]
