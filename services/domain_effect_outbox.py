"""Durable, privacy-safe execution for external domain effects.

The domain mutation and :class:`models.DomainEffectOutbox` row are staged in
the same transaction.  A worker prepares the effect without provider I/O,
persists ``io_started_at`` behind a lease token, and only then invokes the
provider.  Any ambiguous outcome after that boundary becomes ``unknown`` and
is never retried automatically.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Callable, Mapping, Optional
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from models import DomainEffectOutbox, db


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_RECIPIENT_REF_RE = re.compile(
    r"^(?:role|integration|room|projection|recipient_hash|system):"
    r"[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_ERROR_CODE_RE = re.compile(r"[^a-z0-9_.:-]+")
_ERROR_CODE_FULL_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "address",
        "body",
        "contact",
        "credential",
        "description",
        "descripcion",
        "direccion",
        "dni",
        "email",
        "lat",
        "latitude",
        "lng",
        "lon",
        "longitude",
        "media",
        "media_url",
        "message",
        "name",
        "nombre",
        "password",
        "phone",
        "pin",
        "recipient",
        "secret",
        "telefono",
        "text",
        "token",
        "url",
    }
)


class DomainEffectOutboxError(RuntimeError):
    """Base error for fail-closed outbox decisions."""


class DomainEffectValidationError(DomainEffectOutboxError, ValueError):
    """An intent cannot be stored without violating the contract."""


class DomainEffectConflictError(DomainEffectOutboxError):
    """One tenant-scoped effect key was rebound to a different intent."""


class DomainEffectLeaseLostError(DomainEffectOutboxError):
    """A stale worker attempted to update an effect it no longer owns."""


class AmbiguousDomainEffectError(DomainEffectOutboxError):
    """The provider may have accepted I/O but did not return a safe acknowledgement."""


class PermanentDomainEffectError(DomainEffectOutboxError):
    """A preflight failure that must not be retried automatically."""

    def __init__(self, code: str):
        try:
            safe_code = _validated_external_code(
                code,
                field_name="permanent_error_code",
            )
        except DomainEffectValidationError:
            safe_code = "handler_error_redacted"
        super().__init__(safe_code)
        self.code = safe_code


@dataclass(frozen=True)
class DeliveredDomainEffect:
    """Positive provider acknowledgement safe to mark as succeeded."""

    provider_ref: Optional[str] = None
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkippedDomainEffect:
    """Explicit preflight decision proving that no provider I/O is required."""

    reason_code: str
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedDomainEffect:
    """A provider call that may only run after ``io_started_at`` is durable."""

    deliver: Callable[[], DeliveredDomainEffect]


@dataclass(frozen=True)
class DomainEffectClaim:
    effect_id: int
    tenant_id: int
    contract_version: str
    aggregate_type: str
    aggregate_ref: str
    effect_type: str
    handler_name: str
    channel: str
    recipient_ref: str
    effect_key: str
    intent_hmac: str
    payload: Mapping[str, Any]
    attempt_count: int
    max_attempts: int
    lease_token: str


@dataclass(frozen=True)
class DomainEffectStageResult:
    effect: DomainEffectOutbox
    replayed: bool

    @property
    def effect_id(self) -> int:
        return int(self.effect.id)


@dataclass(frozen=True)
class DomainEffectDispatchSummary:
    processed: int
    succeeded: int
    skipped: int
    unknown: int
    retry_wait: int
    dead: int
    recovered_unknown: int = 0
    recovered_retry_wait: int = 0
    recovered_dead: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "contract_version": "domain.effect_dispatch.v1",
            "processed": self.processed,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "unknown": self.unknown,
            "retry_wait": self.retry_wait,
            "dead": self.dead,
            "recovered_unknown": self.recovered_unknown,
            "recovered_retry_wait": self.recovered_retry_wait,
            "recovered_dead": self.recovered_dead,
        }


@dataclass(frozen=True)
class _HandlerRegistration:
    prepare: Callable[[DomainEffectClaim], PreparedDomainEffect | SkippedDomainEffect]
    payload_validator: Optional[Callable[[Mapping[str, Any]], None]] = None


class DomainEffectRegistry:
    """Explicit allowlist of executable effect handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, _HandlerRegistration] = {}

    def register(
        self,
        handler_name: str,
        prepare: Callable[[DomainEffectClaim], PreparedDomainEffect | SkippedDomainEffect],
        payload_validator: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> None:
        normalized = _bounded_identifier(handler_name, "handler_name", 96)
        if not callable(prepare):
            raise DomainEffectValidationError("domain_effect_prepare_invalid")
        if payload_validator is not None and not callable(payload_validator):
            raise DomainEffectValidationError("domain_effect_payload_validator_invalid")
        existing = self._handlers.get(normalized)
        registration = _HandlerRegistration(prepare, payload_validator)
        if existing is not None and existing != registration:
            raise DomainEffectConflictError("domain_effect_handler_already_registered")
        self._handlers[normalized] = registration

    def resolve(self, handler_name: str) -> _HandlerRegistration:
        normalized = _bounded_identifier(handler_name, "handler_name", 96)
        registration = self._handlers.get(normalized)
        if registration is None:
            raise PermanentDomainEffectError("handler_not_registered")
        return registration

    @property
    def handler_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


def compose_domain_effect_registries(
    *registries: DomainEffectRegistry,
) -> DomainEffectRegistry:
    """Return a deterministic, fail-closed union of handler registries.

    The returned registry is always a new object.  Identical registrations may
    be contributed by more than one domain module, while rebinding one handler
    name to a different prepare function or payload validator is rejected by
    :meth:`DomainEffectRegistry.register`.
    """

    composed = DomainEffectRegistry()
    for registry in registries:
        if not isinstance(registry, DomainEffectRegistry):
            raise DomainEffectValidationError(
                "domain_effect_registry_source_invalid"
            )
        for handler_name in registry.handler_names:
            registration = registry.resolve(handler_name)
            composed.register(
                handler_name,
                registration.prepare,
                payload_validator=registration.payload_validator,
            )
    return composed


domain_effect_registry = DomainEffectRegistry()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_code(value: Any) -> str:
    normalized = _ERROR_CODE_RE.sub("_", str(value or "domain_effect_error").strip().lower())
    normalized = normalized.strip("_.:-") or "domain_effect_error"
    return normalized[:64]


def _validated_external_code(value: Any, *, field_name: str) -> str:
    """Accept a code only when it was already a bounded opaque identifier.

    Sanitizing arbitrary handler text into a code can accidentally persist a
    recognizable fragment of an email address, phone number, or citizen text.
    External handlers therefore have to return an actual code, not prose.
    """

    raw = str(value or "").strip().lower()
    if not _ERROR_CODE_FULL_RE.fullmatch(raw):
        raise DomainEffectValidationError(f"domain_effect_{field_name}_invalid")
    return raw


def _error_digest(exc: BaseException | str) -> str:
    if isinstance(exc, BaseException):
        material = f"{type(exc).__name__}:{str(exc)}"
    else:
        material = str(exc)
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _bounded_identifier(value: Any, field_name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or not _IDENTIFIER_RE.fullmatch(normalized)
    ):
        raise DomainEffectValidationError(f"domain_effect_{field_name}_invalid")
    return normalized


def _payload_key_tokens(key: str) -> tuple[str, ...]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return tuple(token for token in re.split(r"[^A-Za-z0-9]+", expanded.lower()) if token)


def _normalize_payload_value(
    value: Any,
    *,
    key_path: tuple[str, ...] = (),
    reject_sensitive_keys: bool = True,
) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise DomainEffectValidationError("domain_effect_payload_number_invalid")
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if len(normalized) > 191 or (normalized and not _IDENTIFIER_RE.fullmatch(normalized)):
            raise DomainEffectValidationError("domain_effect_payload_string_unsafe")
        return normalized
    if isinstance(value, Mapping):
        normalized_map: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key or "").strip()
            if not key or len(key) > 64 or not _IDENTIFIER_RE.fullmatch(key):
                raise DomainEffectValidationError("domain_effect_payload_key_invalid")
            if reject_sensitive_keys and any(
                token in _SENSITIVE_PAYLOAD_KEYS for token in _payload_key_tokens(key)
            ):
                raise DomainEffectValidationError("domain_effect_payload_contains_pii")
            normalized_map[key] = _normalize_payload_value(
                item,
                key_path=(*key_path, key),
                reject_sensitive_keys=reject_sensitive_keys,
            )
        return normalized_map
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise DomainEffectValidationError("domain_effect_payload_list_too_large")
        return [
            _normalize_payload_value(
                item,
                key_path=key_path,
                reject_sensitive_keys=reject_sensitive_keys,
            )
            for item in value
        ]
    raise DomainEffectValidationError("domain_effect_payload_type_invalid")


def _canonical_json(
    value: Any,
    *,
    max_bytes: int = 4096,
    reject_sensitive_keys: bool = True,
) -> tuple[Any, bytes]:
    normalized = _normalize_payload_value(
        value,
        reject_sensitive_keys=reject_sensitive_keys,
    )
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectValidationError("domain_effect_payload_invalid") from exc
    if len(encoded) > max_bytes:
        raise DomainEffectValidationError("domain_effect_payload_too_large")
    return normalized, encoded


def _intent_secret_bytes(intent_secret: Any) -> bytes:
    if isinstance(intent_secret, bytes):
        secret = intent_secret
    else:
        secret = str(intent_secret or "").encode("utf-8")
    if len(secret) < 32:
        raise DomainEffectValidationError("domain_effect_intent_secret_invalid")
    return secret


def _intent_document(
    *,
    contract_version: str = DomainEffectOutbox.CONTRACT_VERSION,
    tenant_id: int,
    aggregate_type: str,
    aggregate_ref: str,
    effect_type: str,
    handler_name: str,
    channel: str,
    recipient_ref: str,
    effect_key: str,
    max_attempts: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "tenant_id": tenant_id,
        "aggregate_type": aggregate_type,
        "aggregate_ref": aggregate_ref,
        "effect_type": effect_type,
        "handler_name": handler_name,
        "channel": channel,
        "recipient_ref": recipient_ref,
        "effect_key": effect_key,
        "max_attempts": max_attempts,
        "payload": payload,
    }


def _intent_hmac(intent: Mapping[str, Any], secret: bytes) -> str:
    _, encoded = _canonical_json(
        intent,
        max_bytes=8192,
        reject_sensitive_keys=False,
    )
    return hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def _existing_row_matches_intent(
    row: DomainEffectOutbox,
    *,
    expected_digest: str,
    intent_secret: bytes,
) -> bool:
    """Verify both the requested replay and the stored immutable row."""

    try:
        stored_intent = _intent_document(
            contract_version=str(row.contract_version),
            tenant_id=int(row.tenant_id),
            aggregate_type=str(row.aggregate_type),
            aggregate_ref=str(row.aggregate_ref),
            effect_type=str(row.effect_type),
            handler_name=str(row.handler_name),
            channel=str(row.channel),
            recipient_ref=str(row.recipient_ref),
            effect_key=str(row.effect_key),
            max_attempts=int(row.max_attempts),
            payload=dict(row.payload_json or {}),
        )
        stored_digest = _intent_hmac(stored_intent, intent_secret)
    except (TypeError, ValueError, DomainEffectValidationError):
        return False
    return hmac.compare_digest(str(row.intent_hmac), stored_digest) and hmac.compare_digest(
        stored_digest,
        expected_digest,
    )


def _validate_recipient_ref(value: Any) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > 191 or not _RECIPIENT_REF_RE.fullmatch(normalized):
        raise DomainEffectValidationError("domain_effect_recipient_ref_invalid")
    prefix, opaque_value = normalized.split(":", 1)
    compact = re.sub(r"[._:-]", "", opaque_value)
    if prefix != "recipient_hash" and len(compact) >= 8 and compact.isdigit():
        raise DomainEffectValidationError("domain_effect_recipient_ref_contains_pii")
    if prefix == "recipient_hash" and not re.fullmatch(r"[0-9a-f]{64}", opaque_value):
        raise DomainEffectValidationError("domain_effect_recipient_hash_invalid")
    return normalized


def stage_domain_effect(
    *,
    tenant_id: int,
    aggregate_type: str,
    aggregate_ref: Any,
    effect_type: str,
    handler_name: str,
    channel: str,
    recipient_ref: str,
    effect_key: str,
    intent_secret: str | bytes,
    registry: DomainEffectRegistry,
    payload: Optional[Mapping[str, Any]] = None,
    max_attempts: int = 8,
    available_at: Optional[datetime] = None,
    max_payload_bytes: int = 4096,
    session: Any = None,
) -> DomainEffectStageResult:
    """Stage one immutable effect without committing the caller transaction."""

    session = session or db.session
    if isinstance(tenant_id, bool):
        raise DomainEffectValidationError("domain_effect_tenant_invalid")
    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectValidationError("domain_effect_tenant_invalid") from exc
    if normalized_tenant_id <= 0:
        raise DomainEffectValidationError("domain_effect_tenant_invalid")

    normalized_aggregate_type = _bounded_identifier(aggregate_type, "aggregate_type", 48)
    normalized_aggregate_ref = _bounded_identifier(aggregate_ref, "aggregate_ref", 191)
    normalized_effect_type = _bounded_identifier(effect_type, "effect_type", 96)
    normalized_handler = _bounded_identifier(handler_name, "handler_name", 96)
    normalized_channel = _bounded_identifier(channel, "channel", 16)
    if normalized_channel not in DomainEffectOutbox.CHANNELS:
        raise DomainEffectValidationError("domain_effect_channel_invalid")
    normalized_recipient = _validate_recipient_ref(recipient_ref)
    normalized_key = _bounded_identifier(effect_key, "effect_key", 191)
    try:
        normalized_max_attempts = int(max_attempts)
        normalized_max_payload_bytes = int(max_payload_bytes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectValidationError("domain_effect_bounds_invalid") from exc
    if normalized_max_attempts < 1 or normalized_max_attempts > 32:
        raise DomainEffectValidationError("domain_effect_max_attempts_invalid")
    if normalized_max_payload_bytes < 256 or normalized_max_payload_bytes > 8192:
        raise DomainEffectValidationError("domain_effect_max_payload_invalid")

    normalized_payload, _ = _canonical_json(
        dict(payload or {}),
        max_bytes=normalized_max_payload_bytes,
    )
    registration = registry.resolve(normalized_handler)
    if registration.payload_validator is not None:
        registration.payload_validator(normalized_payload)

    secret = _intent_secret_bytes(intent_secret)
    intent = _intent_document(
        tenant_id=normalized_tenant_id,
        aggregate_type=normalized_aggregate_type,
        aggregate_ref=normalized_aggregate_ref,
        effect_type=normalized_effect_type,
        handler_name=normalized_handler,
        channel=normalized_channel,
        recipient_ref=normalized_recipient,
        effect_key=normalized_key,
        max_attempts=normalized_max_attempts,
        payload=normalized_payload,
    )
    digest = _intent_hmac(intent, secret)

    existing = session.execute(
        select(DomainEffectOutbox).where(
            DomainEffectOutbox.tenant_id == normalized_tenant_id,
            DomainEffectOutbox.effect_key == normalized_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if _existing_row_matches_intent(
            existing,
            expected_digest=digest,
            intent_secret=secret,
        ):
            return DomainEffectStageResult(effect=existing, replayed=True)
        raise DomainEffectConflictError("domain_effect_payload_conflict")

    values = {
        "tenant_id": normalized_tenant_id,
        "aggregate_type": normalized_aggregate_type,
        "aggregate_ref": normalized_aggregate_ref,
        "effect_type": normalized_effect_type,
        "handler_name": normalized_handler,
        "channel": normalized_channel,
        "recipient_ref": normalized_recipient,
        "effect_key": normalized_key,
        "intent_hmac": digest,
        "payload_json": normalized_payload,
        "status": DomainEffectOutbox.STATUS_PENDING,
        "attempt_count": 0,
        "max_attempts": normalized_max_attempts,
        "available_at": available_at or _utcnow(),
        "contract_version": DomainEffectOutbox.CONTRACT_VERSION,
    }
    dialect_name = str(session.get_bind().dialect.name)
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    elif dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        raise DomainEffectValidationError("domain_effect_database_dialect_unsupported")

    statement = (
        dialect_insert(DomainEffectOutbox)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[
                DomainEffectOutbox.tenant_id,
                DomainEffectOutbox.effect_key,
            ]
        )
        .returning(DomainEffectOutbox.id)
    )
    inserted_id = session.execute(statement).scalar_one_or_none()
    stored = session.execute(
        select(DomainEffectOutbox).where(
            DomainEffectOutbox.tenant_id == normalized_tenant_id,
            DomainEffectOutbox.effect_key == normalized_key,
        )
    ).scalar_one_or_none()
    if stored is None or not _existing_row_matches_intent(
        stored,
        expected_digest=digest,
        intent_secret=secret,
    ):
        raise DomainEffectConflictError("domain_effect_payload_conflict")
    return DomainEffectStageResult(effect=stored, replayed=inserted_id is None)


def _claim_from_row(row: DomainEffectOutbox) -> DomainEffectClaim:
    return DomainEffectClaim(
        effect_id=int(row.id),
        tenant_id=int(row.tenant_id),
        contract_version=str(row.contract_version),
        aggregate_type=str(row.aggregate_type),
        aggregate_ref=str(row.aggregate_ref),
        effect_type=str(row.effect_type),
        handler_name=str(row.handler_name),
        channel=str(row.channel),
        recipient_ref=str(row.recipient_ref),
        effect_key=str(row.effect_key),
        intent_hmac=str(row.intent_hmac),
        payload=dict(row.payload_json or {}),
        attempt_count=int(row.attempt_count),
        max_attempts=int(row.max_attempts),
        lease_token=str(row.lease_token),
    )


def _retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(3600, max(2, 2 ** min(max(attempt_count, 1), 10))))


def recover_stale_domain_effects(
    *,
    tenant_id: Optional[int] = None,
    now: Optional[datetime] = None,
    session: Any = None,
) -> dict[str, int]:
    """Recover expired leases; an expired post-I/O lease is always unknown."""

    session = session or db.session
    operation_now = now or _utcnow()
    query = select(DomainEffectOutbox).where(
        DomainEffectOutbox.status == DomainEffectOutbox.STATUS_PROCESSING,
        DomainEffectOutbox.leased_until.isnot(None),
        DomainEffectOutbox.leased_until <= operation_now,
    )
    if tenant_id is not None:
        query = query.where(DomainEffectOutbox.tenant_id == int(tenant_id))
    rows = session.execute(query.order_by(DomainEffectOutbox.id.asc())).scalars().all()
    counts: Counter[str] = Counter()
    for row in rows:
        if row.io_started_at is not None:
            status = DomainEffectOutbox.STATUS_UNKNOWN
            values = {
                "status": status,
                "lease_token": None,
                "leased_until": None,
                "processed_at": operation_now,
                "last_error_code": "lease_expired_after_io",
                "last_error_digest": _error_digest("lease_expired_after_io"),
            }
        elif int(row.attempt_count) >= int(row.max_attempts):
            status = DomainEffectOutbox.STATUS_DEAD
            values = {
                "status": status,
                "lease_token": None,
                "leased_until": None,
                "processed_at": operation_now,
                "last_error_code": "lease_expired_attempts_exhausted",
                "last_error_digest": _error_digest("lease_expired_attempts_exhausted"),
            }
        else:
            status = DomainEffectOutbox.STATUS_RETRY_WAIT
            values = {
                "status": status,
                "lease_token": None,
                "leased_until": None,
                "processed_at": None,
                "available_at": operation_now + _retry_delay(int(row.attempt_count)),
                "last_error_code": "lease_expired_before_io",
                "last_error_digest": _error_digest("lease_expired_before_io"),
            }
        changed = session.execute(
            update(DomainEffectOutbox)
            .where(
                DomainEffectOutbox.id == row.id,
                DomainEffectOutbox.status == DomainEffectOutbox.STATUS_PROCESSING,
                DomainEffectOutbox.lease_token == row.lease_token,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed:
            counts[status] += 1
    if rows:
        session.commit()
    return {
        "unknown": counts[DomainEffectOutbox.STATUS_UNKNOWN],
        "retry_wait": counts[DomainEffectOutbox.STATUS_RETRY_WAIT],
        "dead": counts[DomainEffectOutbox.STATUS_DEAD],
    }


def _claim_next_domain_effect(
    *,
    tenant_id: Optional[int],
    lease_seconds: int,
    now: datetime,
    session: Any,
) -> Optional[DomainEffectClaim]:
    if lease_seconds < 30 or lease_seconds > 3600:
        raise DomainEffectValidationError("domain_effect_lease_seconds_invalid")
    for _ in range(8):
        due = select(DomainEffectOutbox.id).where(
            DomainEffectOutbox.status.in_(
                (
                    DomainEffectOutbox.STATUS_PENDING,
                    DomainEffectOutbox.STATUS_RETRY_WAIT,
                )
            ),
            DomainEffectOutbox.available_at <= now,
            DomainEffectOutbox.attempt_count < DomainEffectOutbox.max_attempts,
        )
        if tenant_id is not None:
            due = due.where(DomainEffectOutbox.tenant_id == int(tenant_id))
        candidate_id = session.execute(
            due.order_by(DomainEffectOutbox.available_at.asc(), DomainEffectOutbox.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if candidate_id is None:
            session.rollback()
            return None

        lease_token = uuid.uuid4().hex
        changed = session.execute(
            update(DomainEffectOutbox)
            .where(
                DomainEffectOutbox.id == int(candidate_id),
                DomainEffectOutbox.status.in_(
                    (
                        DomainEffectOutbox.STATUS_PENDING,
                        DomainEffectOutbox.STATUS_RETRY_WAIT,
                    )
                ),
                DomainEffectOutbox.available_at <= now,
                DomainEffectOutbox.attempt_count < DomainEffectOutbox.max_attempts,
            )
            .values(
                status=DomainEffectOutbox.STATUS_PROCESSING,
                attempt_count=DomainEffectOutbox.attempt_count + 1,
                lease_token=lease_token,
                leased_until=now + timedelta(seconds=lease_seconds),
                io_started_at=None,
                processed_at=None,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            session.rollback()
            continue
        session.commit()
        row = session.execute(
            select(DomainEffectOutbox).where(
                DomainEffectOutbox.id == int(candidate_id),
                DomainEffectOutbox.status == DomainEffectOutbox.STATUS_PROCESSING,
                DomainEffectOutbox.lease_token == lease_token,
            )
        ).scalar_one_or_none()
        if row is None:
            session.rollback()
            raise DomainEffectLeaseLostError("domain_effect_claim_lost")
        return _claim_from_row(row)
    return None


def _fenced_transition(
    claim: DomainEffectClaim,
    *,
    values: Mapping[str, Any],
    session: Any,
) -> None:
    changed = session.execute(
        update(DomainEffectOutbox)
        .where(
            DomainEffectOutbox.id == claim.effect_id,
            DomainEffectOutbox.tenant_id == claim.tenant_id,
            DomainEffectOutbox.status == DomainEffectOutbox.STATUS_PROCESSING,
            DomainEffectOutbox.lease_token == claim.lease_token,
        )
        .values(**dict(values))
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        session.rollback()
        raise DomainEffectLeaseLostError("domain_effect_lease_lost")
    session.commit()


def _safe_result(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized, _ = _canonical_json(dict(value or {}), max_bytes=4096)
    return dict(normalized)


def _intent_matches_claim(claim: DomainEffectClaim, intent_secret: bytes) -> bool:
    intent = _intent_document(
        contract_version=claim.contract_version,
        tenant_id=claim.tenant_id,
        aggregate_type=claim.aggregate_type,
        aggregate_ref=claim.aggregate_ref,
        effect_type=claim.effect_type,
        handler_name=claim.handler_name,
        channel=claim.channel,
        recipient_ref=claim.recipient_ref,
        effect_key=claim.effect_key,
        max_attempts=claim.max_attempts,
        payload=claim.payload,
    )
    expected = _intent_hmac(intent, intent_secret)
    return hmac.compare_digest(claim.intent_hmac, expected)


def _mark_io_started_if_lease_live(
    claim: DomainEffectClaim,
    *,
    now: datetime,
    session: Any,
) -> bool:
    """Durably cross the I/O boundary only while this exact lease is live."""

    changed = session.execute(
        update(DomainEffectOutbox)
        .where(
            DomainEffectOutbox.id == claim.effect_id,
            DomainEffectOutbox.tenant_id == claim.tenant_id,
            DomainEffectOutbox.status == DomainEffectOutbox.STATUS_PROCESSING,
            DomainEffectOutbox.lease_token == claim.lease_token,
            DomainEffectOutbox.leased_until.isnot(None),
            DomainEffectOutbox.leased_until > now,
            DomainEffectOutbox.io_started_at.is_(None),
        )
        .values(io_started_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        session.rollback()
        return False
    session.commit()
    return True


def _finish_preflight_failure(
    claim: DomainEffectClaim,
    exc: BaseException,
    *,
    now: datetime,
    permanent: bool,
    session: Any,
) -> str:
    exhausted = claim.attempt_count >= claim.max_attempts
    status = (
        DomainEffectOutbox.STATUS_DEAD
        if permanent or exhausted
        else DomainEffectOutbox.STATUS_RETRY_WAIT
    )
    error_code = (
        exc.code
        if isinstance(exc, PermanentDomainEffectError)
        else _safe_error_code(type(exc).__name__)
    )
    values: dict[str, Any] = {
        "status": status,
        "lease_token": None,
        "leased_until": None,
        "last_error_code": error_code,
        "last_error_digest": _error_digest(exc),
    }
    if status == DomainEffectOutbox.STATUS_DEAD:
        values["processed_at"] = now
    else:
        values["processed_at"] = None
        values["available_at"] = now + _retry_delay(claim.attempt_count)
    _fenced_transition(claim, values=values, session=session)
    return status


def _dispatch_claim(
    claim: DomainEffectClaim,
    *,
    registry: DomainEffectRegistry,
    intent_secret: bytes,
    now: datetime,
    lease_clock: Callable[[], datetime],
    session: Any,
) -> Optional[str]:
    if not _intent_matches_claim(claim, intent_secret):
        return _finish_preflight_failure(
            claim,
            PermanentDomainEffectError("intent_hmac_mismatch"),
            now=now,
            permanent=True,
            session=session,
        )
    try:
        registration = registry.resolve(claim.handler_name)
        normalized_payload, _ = _canonical_json(claim.payload, max_bytes=4096)
        if registration.payload_validator is not None:
            registration.payload_validator(normalized_payload)
        prepared = registration.prepare(claim)
        # Preflight is a read-only contract.  Clear any incidental ORM writes
        # before the fenced outbox transition can commit them accidentally.
        session.rollback()
    except PermanentDomainEffectError as exc:
        session.rollback()
        return _finish_preflight_failure(
            claim,
            exc,
            now=now,
            permanent=True,
            session=session,
        )
    except Exception as exc:
        session.rollback()
        return _finish_preflight_failure(
            claim,
            exc,
            now=now,
            permanent=False,
            session=session,
        )

    if isinstance(prepared, SkippedDomainEffect):
        try:
            skip_reason_code = _validated_external_code(
                prepared.reason_code,
                field_name="skip_reason_code",
            )
        except DomainEffectValidationError:
            skip_reason_code = "handler_reason_redacted"
        try:
            safe_skip_result = _safe_result(
                {
                    "reason_code": skip_reason_code,
                    **dict(prepared.result or {}),
                }
            )
        except (DomainEffectValidationError, TypeError, ValueError):
            safe_skip_result = {"reason_code": skip_reason_code}
        _fenced_transition(
            claim,
            values={
                "status": DomainEffectOutbox.STATUS_SKIPPED,
                "lease_token": None,
                "leased_until": None,
                "processed_at": now,
                "result_json": safe_skip_result,
                "last_error_code": None,
                "last_error_digest": None,
            },
            session=session,
        )
        return DomainEffectOutbox.STATUS_SKIPPED
    if not isinstance(prepared, PreparedDomainEffect) or not callable(prepared.deliver):
        return _finish_preflight_failure(
            claim,
            PermanentDomainEffectError("prepared_effect_invalid"),
            now=now,
            permanent=True,
            session=session,
        )

    lease_now = lease_clock()
    if not _mark_io_started_if_lease_live(
        claim,
        now=lease_now,
        session=session,
    ):
        recover_stale_domain_effects(
            tenant_id=claim.tenant_id,
            now=lease_now,
            session=session,
        )
        current_status = session.execute(
            select(DomainEffectOutbox.status).where(
                DomainEffectOutbox.id == claim.effect_id,
                DomainEffectOutbox.tenant_id == claim.tenant_id,
            )
        ).scalar_one_or_none()
        if current_status in {
            DomainEffectOutbox.STATUS_RETRY_WAIT,
            DomainEffectOutbox.STATUS_DEAD,
            DomainEffectOutbox.STATUS_UNKNOWN,
        }:
            return str(current_status)
        return None
    try:
        delivered = prepared.deliver()
        if not isinstance(delivered, DeliveredDomainEffect):
            raise DomainEffectOutboxError("delivery_ack_invalid")
        safe_result = _safe_result(dict(delivered.result or {}))
        provider_ref_hash = (
            hashlib.sha256(str(delivered.provider_ref).encode("utf-8")).hexdigest()
            if delivered.provider_ref
            else None
        )
        # Provider adapters are not allowed to smuggle unrelated DB writes
        # into the acknowledgement transition.
        session.rollback()
    except Exception as exc:
        session.rollback()
        _fenced_transition(
            claim,
            values={
                "status": DomainEffectOutbox.STATUS_UNKNOWN,
                "lease_token": None,
                "leased_until": None,
                "processed_at": _utcnow(),
                "last_error_code": _safe_error_code(type(exc).__name__),
                "last_error_digest": _error_digest(exc),
            },
            session=session,
        )
        return DomainEffectOutbox.STATUS_UNKNOWN

    _fenced_transition(
        claim,
        values={
            "status": DomainEffectOutbox.STATUS_SUCCEEDED,
            "lease_token": None,
            "leased_until": None,
            "processed_at": _utcnow(),
            "provider_ref_hash": provider_ref_hash,
            "result_json": safe_result,
            "last_error_code": None,
            "last_error_digest": None,
        },
        session=session,
    )
    return DomainEffectOutbox.STATUS_SUCCEEDED


def dispatch_domain_effects(
    *,
    registry: DomainEffectRegistry,
    intent_secret: str | bytes,
    tenant_id: Optional[int] = None,
    limit: int = 50,
    lease_seconds: int = 120,
    now: Optional[datetime] = None,
    session: Any = None,
) -> DomainEffectDispatchSummary:
    """Dispatch a bounded batch and never auto-retry an ambiguous send."""

    session = session or db.session
    secret = _intent_secret_bytes(intent_secret)
    operation_now = now or _utcnow()
    lease_clock: Callable[[], datetime] = (
        (lambda: operation_now) if now is not None else _utcnow
    )
    try:
        normalized_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectValidationError("domain_effect_limit_invalid") from exc

    recovered = recover_stale_domain_effects(
        tenant_id=tenant_id,
        now=operation_now,
        session=session,
    )
    counts: Counter[str] = Counter()
    for _ in range(normalized_limit):
        claim = _claim_next_domain_effect(
            tenant_id=tenant_id,
            lease_seconds=int(lease_seconds),
            now=operation_now,
            session=session,
        )
        if claim is None:
            break
        status = _dispatch_claim(
            claim,
            registry=registry,
            intent_secret=secret,
            now=operation_now,
            lease_clock=lease_clock,
            session=session,
        )
        if status is not None:
            counts[status] += 1

    return DomainEffectDispatchSummary(
        processed=sum(counts.values()),
        succeeded=counts[DomainEffectOutbox.STATUS_SUCCEEDED],
        skipped=counts[DomainEffectOutbox.STATUS_SKIPPED],
        unknown=counts[DomainEffectOutbox.STATUS_UNKNOWN],
        retry_wait=counts[DomainEffectOutbox.STATUS_RETRY_WAIT],
        dead=counts[DomainEffectOutbox.STATUS_DEAD],
        recovered_unknown=recovered["unknown"],
        recovered_retry_wait=recovered["retry_wait"],
        recovered_dead=recovered["dead"],
    )


def summarize_domain_effect_outbox(
    *,
    tenant_id: Optional[int] = None,
    now: Optional[datetime] = None,
    session: Any = None,
) -> dict[str, Any]:
    """Return PII-free operational counts for dashboards and alerts."""

    session = session or db.session
    operation_now = now or _utcnow()
    status_query = select(
        DomainEffectOutbox.status,
        func.count(DomainEffectOutbox.id),
    )
    if tenant_id is not None:
        status_query = status_query.where(DomainEffectOutbox.tenant_id == int(tenant_id))
    rows = session.execute(status_query.group_by(DomainEffectOutbox.status)).all()
    counts = {status: 0 for status in DomainEffectOutbox.STATUSES}
    counts.update({str(status): int(count) for status, count in rows})

    due_query = select(func.count(DomainEffectOutbox.id)).where(
        DomainEffectOutbox.status.in_(
            (DomainEffectOutbox.STATUS_PENDING, DomainEffectOutbox.STATUS_RETRY_WAIT)
        ),
        DomainEffectOutbox.available_at <= operation_now,
    )
    stale_query = select(func.count(DomainEffectOutbox.id)).where(
        DomainEffectOutbox.status == DomainEffectOutbox.STATUS_PROCESSING,
        DomainEffectOutbox.leased_until <= operation_now,
    )
    if tenant_id is not None:
        due_query = due_query.where(DomainEffectOutbox.tenant_id == int(tenant_id))
        stale_query = stale_query.where(DomainEffectOutbox.tenant_id == int(tenant_id))
    return {
        "contract_version": "domain.effect_health.v1",
        "by_status": counts,
        "due": int(session.execute(due_query).scalar_one()),
        "stale_processing": int(session.execute(stale_query).scalar_one()),
    }


__all__ = [
    "AmbiguousDomainEffectError",
    "DeliveredDomainEffect",
    "DomainEffectClaim",
    "DomainEffectConflictError",
    "DomainEffectDispatchSummary",
    "DomainEffectLeaseLostError",
    "DomainEffectOutboxError",
    "DomainEffectRegistry",
    "DomainEffectStageResult",
    "DomainEffectValidationError",
    "PermanentDomainEffectError",
    "PreparedDomainEffect",
    "SkippedDomainEffect",
    "compose_domain_effect_registries",
    "dispatch_domain_effects",
    "domain_effect_registry",
    "recover_stale_domain_effects",
    "stage_domain_effect",
    "summarize_domain_effect_outbox",
]
