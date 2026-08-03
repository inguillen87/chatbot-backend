"""Resource guards for the authenticated operational CRM queue.

The queue can legitimately inspect rows that cannot be filtered by portable
SQL (SLA evidence and the legacy ``TenantTicket`` JSON assignee).  This module
keeps that work bounded and reuses the application's shared Flask-Limiter
backend so limits remain tenant/actor scoped across workers when Redis is
configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import time
from typing import Any, Iterable

from flask import current_app
from limits import parse

from extensions import limiter


DEFAULT_RATE_LIMIT = "120 per 60 seconds"
DEFAULT_MAX_SCANNED_ROWS = 5_000
MAX_CONFIGURED_SCANNED_ROWS = 1_000_000
_RATE_LIMIT_NAMESPACE = "crm-operational-queue-v1"


@dataclass(frozen=True)
class QueueRateLimit:
    allowed: bool
    available: bool
    limit: int
    remaining: int
    window_seconds: int
    retry_after_seconds: int
    reset_after_seconds: int


class OperationalQueueGuardError(RuntimeError):
    """Fail-closed resource-policy error safe to expose through the API."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        status_code: int,
        action_hint: str,
        retryable: bool,
        retry_after_seconds: int = 0,
        rate_limit: QueueRateLimit | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status_code = status_code
        self.action_hint = action_hint
        self.retryable = retryable
        self.retry_after_seconds = max(0, int(retry_after_seconds))
        self.rate_limit = rate_limit
        self.details = dict(details or {})


def _positive_identifier(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def _rate_limit_item():
    raw = current_app.config.get(
        "CRM_OPERATIONAL_QUEUE_RATE_LIMIT",
        DEFAULT_RATE_LIMIT,
    )
    try:
        item = parse(str(raw).strip())
        amount = int(item.amount)
        window_seconds = int(item.get_expiry())
    except Exception as exc:
        raise OperationalQueueGuardError(
            "queue_rate_limit_configuration_invalid",
            "La politica de limite de la bandeja operativa no es valida",
            status_code=503,
            action_hint="repair_queue_rate_limit_configuration",
            retryable=False,
            details={"policy": "shared_flask_limiter", "fail_mode": "closed"},
        ) from exc
    if amount <= 0 or amount > 10_000 or window_seconds <= 0 or window_seconds > 86_400:
        raise OperationalQueueGuardError(
            "queue_rate_limit_configuration_invalid",
            "La politica de limite de la bandeja operativa esta fuera de rango",
            status_code=503,
            action_hint="repair_queue_rate_limit_configuration",
            retryable=False,
            details={"policy": "shared_flask_limiter", "fail_mode": "closed"},
        )
    return item, amount, window_seconds


def _rate_scope_key(*, tenant_id: int, actor_id: int) -> str:
    material = f"{tenant_id}\x1f{actor_id}".encode("ascii", "strict")
    return hashlib.sha256(material).hexdigest()


def enforce_operational_queue_rate_limit(
    *,
    tenant_id: Any,
    actor_id: Any,
) -> QueueRateLimit:
    """Consume one shared tenant/actor request slot or fail closed."""

    try:
        normalized_tenant_id = _positive_identifier(tenant_id, field_name="tenant_id")
        normalized_actor_id = _positive_identifier(actor_id, field_name="actor_id")
    except (TypeError, ValueError) as exc:
        raise OperationalQueueGuardError(
            "invalid_queue_rate_scope",
            "No se pudo construir el alcance del limite de la bandeja",
            status_code=503,
            action_hint="repair_queue_actor_scope",
            retryable=False,
            details={"policy": "tenant_actor", "fail_mode": "closed"},
        ) from exc

    item, amount, window_seconds = _rate_limit_item()
    identifiers = (
        _RATE_LIMIT_NAMESPACE,
        _rate_scope_key(
            tenant_id=normalized_tenant_id,
            actor_id=normalized_actor_id,
        ),
    )
    try:
        strategy = limiter.limiter
        allowed = bool(strategy.hit(item, *identifiers))
        window = strategy.get_window_stats(item, *identifiers)
        remaining = max(0, int(window.remaining))
        reset_after = max(1, int(math.ceil(float(window.reset_time) - time.time())))
    except Exception as exc:
        current_app.logger.exception(
            "[crm-operational-queue] Shared rate limiter unavailable; request denied"
        )
        unavailable = QueueRateLimit(
            allowed=False,
            available=False,
            limit=amount,
            remaining=0,
            window_seconds=window_seconds,
            retry_after_seconds=window_seconds,
            reset_after_seconds=window_seconds,
        )
        raise OperationalQueueGuardError(
            "queue_rate_limit_unavailable",
            "El control compartido de capacidad de la bandeja no esta disponible",
            status_code=503,
            action_hint="retry_after_rate_limit_recovers",
            retryable=True,
            retry_after_seconds=window_seconds,
            rate_limit=unavailable,
            details={"policy": "shared_flask_limiter", "fail_mode": "closed"},
        ) from exc

    decision = QueueRateLimit(
        allowed=allowed,
        available=True,
        limit=amount,
        remaining=remaining,
        window_seconds=window_seconds,
        retry_after_seconds=0 if allowed else reset_after,
        reset_after_seconds=reset_after,
    )
    if not allowed:
        raise OperationalQueueGuardError(
            "queue_rate_limited",
            "Se alcanzo el limite temporal de consultas de la bandeja operativa",
            status_code=429,
            action_hint="retry_after_rate_limit_reset",
            retryable=True,
            retry_after_seconds=reset_after,
            rate_limit=decision,
            details={"policy": "tenant_actor", "fail_mode": "closed"},
        )
    return decision


def configured_queue_scan_budget(
    *,
    post_query_filters: Iterable[str] = (),
) -> "QueueScanBudget":
    raw = current_app.config.get(
        "CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS",
        DEFAULT_MAX_SCANNED_ROWS,
    )
    try:
        max_rows = int(raw)
    except (TypeError, ValueError) as exc:
        raise OperationalQueueGuardError(
            "queue_scan_budget_configuration_invalid",
            "El presupuesto de inspeccion de la bandeja no es valido",
            status_code=503,
            action_hint="repair_queue_scan_budget_configuration",
            retryable=False,
            details={"fail_mode": "closed"},
        ) from exc
    if max_rows <= 0 or max_rows > MAX_CONFIGURED_SCANNED_ROWS:
        raise OperationalQueueGuardError(
            "queue_scan_budget_configuration_invalid",
            "El presupuesto de inspeccion de la bandeja esta fuera de rango",
            status_code=503,
            action_hint="repair_queue_scan_budget_configuration",
            retryable=False,
            details={"fail_mode": "closed"},
        )
    return QueueScanBudget(
        max_rows=max_rows,
        post_query_filters=tuple(dict.fromkeys(post_query_filters)),
    )


@dataclass
class QueueScanBudget:
    """Shared logical-row inspection budget for all queue source streams."""

    max_rows: int
    post_query_filters: tuple[str, ...] = field(default_factory=tuple)
    inspected_rows: int = 0

    def consume(self, *, source_model: str) -> None:
        next_count = self.inspected_rows + 1
        if next_count > self.max_rows:
            raise OperationalQueueGuardError(
                "queue_scan_budget_exceeded",
                "La consulta supero el presupuesto seguro de inspeccion; aplica filtros mas selectivos",
                status_code=503,
                action_hint="narrow_queue_filters_or_retry",
                retryable=True,
                retry_after_seconds=1,
                details={
                    "max_inspected_rows": self.max_rows,
                    "inspected_rows_before_abort": self.inspected_rows,
                    "source_model_at_abort": str(source_model),
                    "post_query_filters": list(self.post_query_filters),
                    "partial_page_returned": False,
                },
            )
        self.inspected_rows = next_count


def attach_operational_queue_rate_limit_headers(
    response: Any,
    rate_limit: QueueRateLimit | None,
    *,
    retry_after_seconds: int = 0,
):
    if rate_limit is not None:
        response.headers["X-RateLimit-Limit"] = str(rate_limit.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_limit.remaining)
        response.headers["X-RateLimit-Window"] = str(rate_limit.window_seconds)
        response.headers["X-RateLimit-Reset-After"] = str(rate_limit.reset_after_seconds)
    if retry_after_seconds > 0:
        response.headers["Retry-After"] = str(max(1, int(retry_after_seconds)))
    return response


__all__ = [
    "OperationalQueueGuardError",
    "QueueRateLimit",
    "QueueScanBudget",
    "attach_operational_queue_rate_limit_headers",
    "configured_queue_scan_budget",
    "enforce_operational_queue_rate_limit",
]
