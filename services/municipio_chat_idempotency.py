"""Durable exactly-once admission and replay for municipal HTTP chat turns.

The LLM and action-handler architecture stays unchanged.  This module only
guards execution at the HTTP boundary, before the LLM is called, and stores a
completed response snapshot after all normal chat persistence succeeds.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from flask import current_app
from sqlalchemy import null, text
from sqlalchemy.exc import IntegrityError

from models import MunicipioChatIdempotencyReceipt, db


CONTRACT_VERSION = MunicipioChatIdempotencyReceipt.CONTRACT_VERSION
CANONICAL_ENDPOINT = "/api/ask/municipio"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


class MunicipioChatIdempotencyError(RuntimeError):
    """Base error for fail-closed municipal chat idempotency decisions."""


class InvalidIdempotencyKey(MunicipioChatIdempotencyError, ValueError):
    reason_code = "invalid_idempotency_key"


class IdempotencyPayloadConflict(MunicipioChatIdempotencyError):
    reason_code = "municipio_chat_idempotency_payload_conflict"


class IdempotencyRequestInProgress(MunicipioChatIdempotencyError):
    reason_code = "municipio_chat_idempotency_in_progress"


class IdempotencyReplayUnavailable(MunicipioChatIdempotencyError):
    reason_code = "municipio_chat_idempotency_replay_unavailable"


class IdempotencyResponseExpired(MunicipioChatIdempotencyError):
    reason_code = "municipio_chat_idempotency_response_expired"


class IdempotencyScopeConflict(MunicipioChatIdempotencyError):
    reason_code = "municipio_chat_idempotency_scope_conflict"


@dataclass(frozen=True)
class IdempotencyIdentity:
    tenant_id: int
    endpoint: str
    actor_scope_hash: str
    idempotency_key_hash: str
    request_hash: str

    @property
    def lock_digest(self) -> str:
        material = ":".join(
            (
                str(self.tenant_id),
                self.endpoint,
                self.actor_scope_hash,
                self.idempotency_key_hash,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyDecision:
    receipt_id: int
    replayed: bool


_local_locks_guard = threading.Lock()
_local_locks: dict[str, tuple[threading.Lock, int]] = {}
_retention_sweep_guard = threading.Lock()
_retention_next_sweep_monotonic = 0.0


def validate_idempotency_key(raw_key: Any) -> str:
    key = str(raw_key or "").strip()
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise InvalidIdempotencyKey(
            "Idempotency-Key debe tener entre 8 y 128 caracteres seguros."
        )
    return key


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_actor_scope_hash(*, actor_kind: str, actor_id: Any) -> str:
    normalized_kind = str(actor_kind or "").strip().lower()
    normalized_id = str(actor_id or "").strip()
    if normalized_kind not in {"user", "session"} or not normalized_id:
        raise ValueError("Municipal chat idempotency requires a stable actor or session.")
    return sha256_text(f"{normalized_kind}:{normalized_id}")


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON numbers are not supported.")
        return value
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return str(value)


def canonical_request_hash(
    *,
    endpoint: str,
    query_items: list[tuple[str, str]],
    json_payload: Any = None,
    raw_body: bytes | None = None,
    form_items: list[tuple[str, list[str]]] | None = None,
    file_items: list[dict[str, Any]] | None = None,
) -> str:
    """Hash one semantic request without retaining citizen data or credentials."""

    if json_payload is not None:
        body_contract: Any = {"kind": "json", "value": _canonicalize(json_payload)}
    elif form_items is not None or file_items is not None:
        body_contract = {
            "kind": "multipart_or_form",
            "form": [
                [str(key), [_canonicalize(value) for value in values]]
                for key, values in sorted(form_items or [], key=lambda pair: pair[0])
            ],
            # Flask observes upload order within one field (for example through
            # ``getlist`` or first-file selection).  Sort field names, but never
            # sort duplicate files inside a field.
            "files": [
                [
                    field,
                    [
                        _canonicalize(item)
                        for item in (file_items or [])
                        if str(item.get("field") or "") == field
                    ],
                ]
                for field in sorted(
                    {
                        str(item.get("field") or "")
                        for item in (file_items or [])
                    }
                )
            ],
        }
    else:
        body_contract = {
            "kind": "raw",
            "sha256": hashlib.sha256(raw_body or b"").hexdigest(),
        }

    canonical = {
        "method": "POST",
        "endpoint": endpoint,
        # Distinct query-key order is not semantic, while duplicate value order
        # is: Werkzeug's ``MultiDict.get`` returns the first value.
        "query": [
            [
                key,
                [
                    str(value)
                    for item_key, value in query_items
                    if str(item_key) == key
                ],
            ]
            for key in sorted({str(key) for key, _value in query_items})
        ],
        "body": body_contract,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_identity(
    *,
    tenant_id: Any,
    actor_kind: str,
    actor_id: Any,
    idempotency_key: str,
    request_hash: str,
    endpoint: str = CANONICAL_ENDPOINT,
) -> IdempotencyIdentity:
    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Municipal chat idempotency requires a tenant.") from exc
    if isinstance(tenant_id, bool) or normalized_tenant_id <= 0:
        raise ValueError("Municipal chat idempotency requires a tenant.")
    if not re.fullmatch(r"[0-9a-f]{64}", str(request_hash or "")):
        raise ValueError("Invalid canonical request hash.")
    normalized_key = validate_idempotency_key(idempotency_key)
    return IdempotencyIdentity(
        tenant_id=normalized_tenant_id,
        endpoint=str(endpoint or CANONICAL_ENDPOINT)[:80],
        actor_scope_hash=build_actor_scope_hash(
            actor_kind=actor_kind,
            actor_id=actor_id,
        ),
        idempotency_key_hash=sha256_text(normalized_key),
        request_hash=request_hash,
    )


def _postgres_lock_id(lock_digest: str) -> int:
    return int.from_bytes(
        bytes.fromhex(lock_digest)[:8],
        byteorder="big",
        signed=True,
    )


@contextmanager
def _local_lock(lock_digest: str, timeout_seconds: float) -> Iterator[None]:
    with _local_locks_guard:
        lock, users = _local_locks.get(lock_digest, (threading.Lock(), 0))
        _local_locks[lock_digest] = (lock, users + 1)
    acquired = lock.acquire(timeout=max(0.0, timeout_seconds))
    if not acquired:
        with _local_locks_guard:
            current_lock, current_users = _local_locks.get(lock_digest, (lock, 1))
            if current_lock is lock and current_users <= 1:
                _local_locks.pop(lock_digest, None)
            elif current_lock is lock:
                _local_locks[lock_digest] = (lock, current_users - 1)
        raise IdempotencyRequestInProgress("The idempotency key is still processing.")
    try:
        yield
    finally:
        lock.release()
        with _local_locks_guard:
            current_lock, current_users = _local_locks.get(lock_digest, (lock, 1))
            if current_lock is lock and current_users <= 1:
                _local_locks.pop(lock_digest, None)
            elif current_lock is lock:
                _local_locks[lock_digest] = (lock, current_users - 1)


@contextmanager
def execution_lock(identity: IdempotencyIdentity) -> Iterator[None]:
    """Serialize one scope across threads and PostgreSQL application instances."""

    try:
        timeout_seconds = float(
            current_app.config.get(
                "MUNICIPIO_CHAT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS",
                20,
            )
        )
    except (TypeError, ValueError):
        timeout_seconds = 20.0
    timeout_seconds = max(0.1, min(timeout_seconds, 60.0))
    with _local_lock(identity.lock_digest, timeout_seconds):
        bind = db.session.get_bind()
        dialect = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect != "postgresql":
            yield
            return

        lock_id = _postgres_lock_id(identity.lock_digest)
        deadline = time.monotonic() + timeout_seconds
        connection = db.engine.connect()
        transaction = connection.begin()
        acquired = False
        try:
            while time.monotonic() < deadline:
                acquired = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                        {"lock_id": lock_id},
                    ).scalar()
                )
                if acquired:
                    break
                time.sleep(0.05)
            if not acquired:
                raise IdempotencyRequestInProgress(
                    "The idempotency key is still processing."
                )
            yield
            transaction.commit()
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            raise
        finally:
            # Transaction-scoped advisory locks are released by commit/rollback.
            # This is safe with Neon/PgBouncer transaction pooling and cannot
            # leak a session-level lock when the pooled connection is returned.
            if transaction.is_active:
                transaction.rollback()
            connection.close()


def expire_completed_response_snapshots(
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Scrub old response bodies while retaining no-reexecution tombstones.

    The bounded, portable ORM batch works on PostgreSQL/Neon and SQLite.  It is
    safe for multiple workers: each transition is monotonic and an expired row
    can never be reclaimed for execution.
    """

    retention_days = int(
        current_app.config.get(
            "MUNICIPIO_CHAT_IDEMPOTENCY_RESPONSE_RETENTION_DAYS",
            30,
        )
    )
    batch_size = int(
        current_app.config.get(
            "MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_BATCH_SIZE",
            100,
        )
    )
    retention_days = max(1, min(retention_days, 90))
    batch_size = max(1, min(batch_size, 500))
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=retention_days)
    receipts = (
        MunicipioChatIdempotencyReceipt.query.filter_by(
            status=MunicipioChatIdempotencyReceipt.STATUS_COMPLETED
        )
        .filter(MunicipioChatIdempotencyReceipt.completed_at < cutoff)
        .order_by(MunicipioChatIdempotencyReceipt.completed_at.asc())
        .limit(batch_size)
        .all()
    )
    for receipt in receipts:
        receipt.status = MunicipioChatIdempotencyReceipt.STATUS_EXPIRED
        # SQLAlchemy JSON encodes Python ``None`` as JSON ``null`` on SQLite;
        # the tombstone invariant requires a real SQL NULL on both dialects.
        receipt.response_json = null()
        receipt.response_status = None
        receipt.response_request_id = None
        receipt.expired_at = current_time
        receipt.updated_at = current_time
        db.session.add(receipt)
    if receipts and commit:
        db.session.commit()
    return len(receipts)


def maybe_expire_completed_response_snapshots() -> int:
    """Run at most one bounded retention batch per configured process interval."""

    global _retention_next_sweep_monotonic
    now_monotonic = time.monotonic()
    with _retention_sweep_guard:
        if now_monotonic < _retention_next_sweep_monotonic:
            return 0
        sweep_seconds = int(
            current_app.config.get(
                "MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_SWEEP_SECONDS",
                300,
            )
        )
        sweep_seconds = max(60, min(sweep_seconds, 3600))
        _retention_next_sweep_monotonic = now_monotonic + sweep_seconds

    try:
        return expire_completed_response_snapshots()
    except Exception as exc:  # pragma: no cover - cleanup must not break chat
        db.session.rollback()
        current_app.logger.warning(
            "Municipal idempotency retention sweep skipped error_type=%s",
            type(exc).__name__,
        )
        return 0


def _receipt_query(identity: IdempotencyIdentity):
    return MunicipioChatIdempotencyReceipt.query.filter_by(
        tenant_id=identity.tenant_id,
        endpoint=identity.endpoint,
        actor_scope_hash=identity.actor_scope_hash,
        idempotency_key_hash=identity.idempotency_key_hash,
    )


def claim_or_replay(identity: IdempotencyIdentity) -> IdempotencyDecision:
    """Reserve a new key, replay a completed key, or fail closed."""

    receipt = _receipt_query(identity).one_or_none()
    if receipt is not None:
        if not hmac.compare_digest(receipt.request_hash, identity.request_hash):
            raise IdempotencyPayloadConflict(
                "Idempotency-Key ya fue usado con otro payload en esta sesion."
            )
        if receipt.status == MunicipioChatIdempotencyReceipt.STATUS_COMPLETED:
            if receipt.response_json is None or receipt.response_status is None:
                raise IdempotencyReplayUnavailable(
                    "The completed idempotency receipt has no response snapshot."
                )
            return IdempotencyDecision(receipt_id=receipt.id, replayed=True)
        if receipt.status == MunicipioChatIdempotencyReceipt.STATUS_EXPIRED:
            raise IdempotencyResponseExpired(
                "La respuesta asociada a esta Idempotency-Key ya fue depurada. "
                "La operacion no se ejecutara nuevamente."
            )
        raise IdempotencyRequestInProgress(
            "The prior execution did not reach a replayable response."
        )

    receipt = MunicipioChatIdempotencyReceipt(
        tenant_id=identity.tenant_id,
        endpoint=identity.endpoint,
        actor_scope_hash=identity.actor_scope_hash,
        idempotency_key_hash=identity.idempotency_key_hash,
        request_hash=identity.request_hash,
        status=MunicipioChatIdempotencyReceipt.STATUS_PROCESSING,
    )
    db.session.add(receipt)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = _receipt_query(identity).one_or_none()
        if winner is None:
            raise
        if not hmac.compare_digest(winner.request_hash, identity.request_hash):
            raise IdempotencyPayloadConflict(
                "Idempotency-Key ya fue usado con otro payload en esta sesion."
            )
        if winner.status == MunicipioChatIdempotencyReceipt.STATUS_COMPLETED:
            return IdempotencyDecision(receipt_id=winner.id, replayed=True)
        if winner.status == MunicipioChatIdempotencyReceipt.STATUS_EXPIRED:
            raise IdempotencyResponseExpired(
                "La respuesta asociada a esta Idempotency-Key ya fue depurada. "
                "La operacion no se ejecutara nuevamente."
            )
        raise IdempotencyRequestInProgress(
            "A concurrent request already reserved this idempotency key."
        )
    return IdempotencyDecision(receipt_id=receipt.id, replayed=False)


def complete_receipt(
    receipt_id: int,
    *,
    response_json: Any,
    response_status: int,
    response_request_id: str | None,
    commit: bool = True,
) -> MunicipioChatIdempotencyReceipt:
    """Stage or commit an exact JSON replay snapshot for one active claim."""

    receipt = db.session.get(MunicipioChatIdempotencyReceipt, int(receipt_id))
    if receipt is None:
        raise IdempotencyReplayUnavailable("Idempotency receipt disappeared.")
    if receipt.status == MunicipioChatIdempotencyReceipt.STATUS_COMPLETED:
        current_digest = hashlib.sha256(
            json.dumps(
                _canonicalize(receipt.response_json),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        candidate_digest = hashlib.sha256(
            json.dumps(
                _canonicalize(response_json),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            int(receipt.response_status or 0) != int(response_status)
            or not hmac.compare_digest(current_digest, candidate_digest)
        ):
            raise IdempotencyReplayUnavailable(
                "Completed idempotency snapshot changed before HTTP delivery."
            )
        return receipt
    if receipt.status == MunicipioChatIdempotencyReceipt.STATUS_EXPIRED:
        raise IdempotencyResponseExpired(
            "An expired idempotency tombstone cannot be completed again."
        )
    if receipt.status != MunicipioChatIdempotencyReceipt.STATUS_PROCESSING:
        raise IdempotencyReplayUnavailable(
            "The idempotency receipt is not in a completable state."
        )
    normalized_status = int(response_status)
    if normalized_status < 100 or normalized_status > 599:
        raise ValueError("Invalid HTTP response status for idempotency receipt.")
    if not isinstance(response_json, (dict, list)):
        raise ValueError("Municipal chat idempotency only stores JSON responses.")

    receipt.response_json = response_json
    receipt.response_status = normalized_status
    receipt.response_request_id = str(response_request_id or "").strip()[:128] or None
    receipt.status = MunicipioChatIdempotencyReceipt.STATUS_COMPLETED
    receipt.completed_at = datetime.now(timezone.utc)
    receipt.updated_at = datetime.now(timezone.utc)
    db.session.add(receipt)
    if commit:
        db.session.commit()
    return receipt


def discard_processing_receipt(receipt_id: int) -> None:
    """Remove a reservation only when execution was rejected before effects."""

    receipt = db.session.get(MunicipioChatIdempotencyReceipt, int(receipt_id))
    if receipt is None:
        return
    if receipt.status != MunicipioChatIdempotencyReceipt.STATUS_PROCESSING:
        raise IdempotencyReplayUnavailable(
            "A non-processing idempotency receipt cannot be discarded."
        )
    db.session.delete(receipt)
    db.session.commit()


def replay_snapshot(receipt_id: int) -> tuple[Any, int, str | None]:
    receipt = db.session.get(MunicipioChatIdempotencyReceipt, int(receipt_id))
    if (
        receipt is None
        or receipt.status != MunicipioChatIdempotencyReceipt.STATUS_COMPLETED
        or receipt.response_json is None
        or receipt.response_status is None
    ):
        raise IdempotencyReplayUnavailable(
            "The idempotency receipt is not replayable."
        )
    return receipt.response_json, int(receipt.response_status), receipt.response_request_id


__all__ = [
    "CANONICAL_ENDPOINT",
    "CONTRACT_VERSION",
    "IdempotencyPayloadConflict",
    "IdempotencyReplayUnavailable",
    "IdempotencyRequestInProgress",
    "IdempotencyResponseExpired",
    "IdempotencyScopeConflict",
    "InvalidIdempotencyKey",
    "build_identity",
    "canonical_request_hash",
    "claim_or_replay",
    "complete_receipt",
    "discard_processing_receipt",
    "expire_completed_response_snapshots",
    "maybe_expire_completed_response_snapshots",
    "execution_lock",
    "replay_snapshot",
    "validate_idempotency_key",
]
