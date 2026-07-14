"""Durable, tenant-agnostic webhook delivery claims.

Claims are committed before application code processes a webhook. Completion and
failure transitions use the claim attempt as a fencing token so an expired worker
cannot overwrite a newer worker's result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import db
from models import WebhookDelivery


CLAIMED = "claimed"
DUPLICATE = "duplicate"
IN_PROGRESS = "in_progress"

DEFAULT_STALE_AFTER = timedelta(minutes=5)
MAX_LAST_ERROR_LENGTH = 1000

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|api[-_ ]?key|client_secret|secret|token|password)"
    r"[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?[^\s,;\"'}]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")


class WebhookDeliveryValidationError(ValueError):
    """Raised when a webhook identity or transition token is invalid."""


class WebhookDeliveryStateError(RuntimeError):
    """Raised when persisted delivery state violates the model contract."""


@dataclass(frozen=True)
class WebhookDeliveryClaim:
    outcome: str
    delivery_id: int
    provider: str
    event_id: str
    event_type: str
    payload_digest: str
    attempts: int

    @property
    def should_process(self) -> bool:
        return self.outcome == CLAIMED

    @property
    def is_retry(self) -> bool:
        return self.should_process and self.attempts > 1


def _normalize_identifier(
    value: Any,
    *,
    field: str,
    max_length: int,
    lowercase: bool = False,
) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WebhookDeliveryValidationError(f"{field} must be a string or integer")

    normalized = str(value).strip()
    if lowercase:
        normalized = normalized.lower()
    if not normalized:
        raise WebhookDeliveryValidationError(f"{field} is required")
    if len(normalized) > max_length:
        raise WebhookDeliveryValidationError(
            f"{field} exceeds the {max_length}-character limit"
        )
    if any(character.isspace() or not character.isprintable() for character in normalized):
        raise WebhookDeliveryValidationError(
            f"{field} must not contain whitespace or control characters"
        )
    return normalized


def _normalize_delivery_id(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise WebhookDeliveryValidationError(f"{field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise WebhookDeliveryValidationError(
            f"{field} must be a positive integer"
        ) from exc
    if normalized < 1:
        raise WebhookDeliveryValidationError(f"{field} must be a positive integer")
    return normalized


def _normalize_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime):
        raise WebhookDeliveryValidationError("now must be a datetime")
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _normalize_stale_after(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise WebhookDeliveryValidationError("stale_after must be a positive timedelta")
    return value


def _digest_payload(payload: bytes | bytearray | memoryview | str) -> str:
    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        payload_bytes = payload
    elif isinstance(payload, (bytearray, memoryview)):
        payload_bytes = bytes(payload)
    else:
        raise WebhookDeliveryValidationError(
            "payload must be raw bytes or a string"
        )
    return hashlib.sha256(payload_bytes).hexdigest()


def _sanitize_last_error(error: Any) -> str:
    if isinstance(error, BaseException):
        raw = f"{error.__class__.__name__}: {error}"
    else:
        raw = str(error or "")

    normalized = " ".join(raw.split()).strip() or "webhook_processing_failed"
    if normalized.startswith(("{", "[")) or len(normalized) > MAX_LAST_ERROR_LENGTH:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return f"error_sha256:{digest}"

    normalized = _BEARER_RE.sub("Bearer [REDACTED]", normalized)
    normalized = _JWT_RE.sub("[REDACTED_JWT]", normalized)
    normalized = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        normalized,
    )
    return normalized[:MAX_LAST_ERROR_LENGTH]


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _get_delivery(
    session: Session,
    provider: str,
    event_id: str,
) -> WebhookDelivery | None:
    return session.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.provider == provider,
            WebhookDelivery.event_id == event_id,
        )
    )


def _claim_from_delivery(
    delivery: WebhookDelivery,
    outcome: str,
) -> WebhookDeliveryClaim:
    return WebhookDeliveryClaim(
        outcome=outcome,
        delivery_id=delivery.id,
        provider=delivery.provider,
        event_id=delivery.event_id,
        event_type=delivery.event_type,
        payload_digest=delivery.payload_digest,
        attempts=delivery.attempts,
    )


def _claim_existing(
    session: Session,
    delivery: WebhookDelivery,
    *,
    event_type: str,
    payload_digest: str,
    now: datetime,
    stale_before: datetime,
) -> WebhookDeliveryClaim:
    current = delivery

    for _ in range(3):
        if current.status == WebhookDelivery.STATUS_PROCESSED:
            result = _claim_from_delivery(current, DUPLICATE)
            session.rollback()
            return result

        if current.status == WebhookDelivery.STATUS_PROCESSING:
            if _as_utc(current.updated_at) > stale_before:
                result = _claim_from_delivery(current, IN_PROGRESS)
                session.rollback()
                return result
        elif current.status != WebhookDelivery.STATUS_FAILED:
            session.rollback()
            raise WebhookDeliveryStateError(
                f"unsupported webhook delivery status: {current.status!r}"
            )

        retry = session.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == current.id,
                or_(
                    WebhookDelivery.status == WebhookDelivery.STATUS_FAILED,
                    and_(
                        WebhookDelivery.status == WebhookDelivery.STATUS_PROCESSING,
                        WebhookDelivery.updated_at <= stale_before,
                    ),
                ),
            )
            .values(
                event_type=event_type,
                payload_digest=payload_digest,
                status=WebhookDelivery.STATUS_PROCESSING,
                attempts=WebhookDelivery.attempts + 1,
                processed_at=None,
                last_error=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if retry.rowcount == 1:
            result = WebhookDeliveryClaim(
                outcome=CLAIMED,
                delivery_id=current.id,
                provider=current.provider,
                event_id=current.event_id,
                event_type=event_type,
                payload_digest=payload_digest,
                attempts=current.attempts + 1,
            )
            session.commit()
            return result

        session.rollback()
        refreshed = _get_delivery(session, current.provider, current.event_id)
        if refreshed is None:
            session.rollback()
            raise WebhookDeliveryStateError(
                "webhook delivery disappeared during claim arbitration"
            )
        current = refreshed

    result = _claim_from_delivery(
        current,
        DUPLICATE if current.status == WebhookDelivery.STATUS_PROCESSED else IN_PROGRESS,
    )
    session.rollback()
    return result


def claim_delivery(
    provider: str,
    event_id: str | int,
    event_type: str,
    payload: bytes | bytearray | memoryview | str,
    *,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    now: datetime | None = None,
) -> WebhookDeliveryClaim:
    """Claim a webhook event, or report its already-known state."""

    normalized_provider = _normalize_identifier(
        provider,
        field="provider",
        max_length=64,
        lowercase=True,
    )
    normalized_event_id = _normalize_identifier(
        event_id,
        field="event_id",
        max_length=255,
    )
    normalized_event_type = _normalize_identifier(
        event_type,
        field="event_type",
        max_length=128,
    )
    payload_digest = _digest_payload(payload)
    current_time = _normalize_now(now)
    stale_before = current_time - _normalize_stale_after(stale_after)

    with Session(bind=db.engine, expire_on_commit=False) as session:
        delivery = _get_delivery(session, normalized_provider, normalized_event_id)
        if delivery is not None:
            return _claim_existing(
                session,
                delivery,
                event_type=normalized_event_type,
                payload_digest=payload_digest,
                now=current_time,
                stale_before=stale_before,
            )

        delivery = WebhookDelivery(
            provider=normalized_provider,
            event_id=normalized_event_id,
            event_type=normalized_event_type,
            payload_digest=payload_digest,
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
            delivery = _get_delivery(
                session,
                normalized_provider,
                normalized_event_id,
            )
            if delivery is None:
                raise
            return _claim_existing(
                session,
                delivery,
                event_type=normalized_event_type,
                payload_digest=payload_digest,
                now=current_time,
                stale_before=stale_before,
            )

        return _claim_from_delivery(delivery, CLAIMED)


def stage_delivery_completion(
    session: Session,
    delivery_id: int,
    attempt: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Stage a fenced completion in the caller's current transaction."""

    normalized_id = _normalize_delivery_id(delivery_id, field="delivery_id")
    normalized_attempt = _normalize_delivery_id(attempt, field="attempt")
    current_time = _normalize_now(now)

    transition = session.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == normalized_id,
            WebhookDelivery.status == WebhookDelivery.STATUS_PROCESSING,
            WebhookDelivery.attempts == normalized_attempt,
        )
        .values(
            status=WebhookDelivery.STATUS_PROCESSED,
            processed_at=current_time,
            last_error=None,
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
    )
    return transition.rowcount == 1


def complete_delivery(
    delivery_id: int,
    attempt: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Mark the matching active attempt processed; stale attempts return False."""

    normalized_id = _normalize_delivery_id(delivery_id, field="delivery_id")
    normalized_attempt = _normalize_delivery_id(attempt, field="attempt")

    with Session(bind=db.engine, expire_on_commit=False) as session:
        staged = stage_delivery_completion(
            session,
            normalized_id,
            normalized_attempt,
            now=now,
        )
        if staged:
            session.commit()
            return True

        session.rollback()
        current = session.get(WebhookDelivery, normalized_id)
        already_completed = bool(
            current
            and current.status == WebhookDelivery.STATUS_PROCESSED
            and current.attempts == normalized_attempt
        )
        session.rollback()
        return already_completed


def fail_delivery(
    delivery_id: int,
    attempt: int,
    last_error: Any,
    *,
    now: datetime | None = None,
) -> bool:
    """Fail the matching active attempt without persisting payloads or secrets."""

    normalized_id = _normalize_delivery_id(delivery_id, field="delivery_id")
    normalized_attempt = _normalize_delivery_id(attempt, field="attempt")
    current_time = _normalize_now(now)
    safe_error = _sanitize_last_error(last_error)

    with Session(bind=db.engine, expire_on_commit=False) as session:
        transition = session.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == normalized_id,
                WebhookDelivery.status == WebhookDelivery.STATUS_PROCESSING,
                WebhookDelivery.attempts == normalized_attempt,
            )
            .values(
                status=WebhookDelivery.STATUS_FAILED,
                processed_at=None,
                last_error=safe_error,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if transition.rowcount == 1:
            session.commit()
            return True

        session.rollback()
        current = session.get(WebhookDelivery, normalized_id)
        already_failed = bool(
            current
            and current.status == WebhookDelivery.STATUS_FAILED
            and current.attempts == normalized_attempt
        )
        session.rollback()
        return already_failed
