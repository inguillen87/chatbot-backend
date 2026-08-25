"""Bounded, at-most-once weekly analytics report generation.

Every provider attempt is fenced twice before network I/O: an atomic Redis
reservation closes the concurrency race and a committed AnalyticsEvent keeps
the attempt idempotent after the Redis lease expires.  A crash after provider
I/O therefore leaves an intentionally non-retriable reservation instead of
risking a duplicate paid request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select, text

from database import db
from models import AnalyticsEvent, TenantProfile
from services.analytics_service import analytics_service


logger = logging.getLogger(__name__)

WEEKLY_ANALYTICS_RUN_CONTRACT_VERSION = "weekly.analytics_report_run.v1"
WEEKLY_ANALYTICS_DRAIN_CONTRACT_VERSION = "weekly.analytics_report_drain.v1"
WEEKLY_ANALYTICS_RESERVATION_CONTRACT_VERSION = (
    "weekly.analytics_provider_reservation.v1"
)
WEEKLY_ANALYTICS_CACHE_CONTRACT_VERSION = "weekly.analytics_report_cache.v1"
DEFAULT_MAX_TENANTS_PER_RUN = 5
MAX_TENANTS_PER_RUN = 10
DEFAULT_MAX_BATCHES_PER_DRAIN = 12
MAX_BATCHES_PER_DRAIN = 24
DEFAULT_RESERVATION_TTL_SECONDS = 900
MIN_RESERVATION_TTL_SECONDS = 60
MAX_RESERVATION_TTL_SECONDS = 3600
_SUPPORTED_TENANT_TYPES = ("municipio", "pyme")
_MAX_PROVIDER_INPUT_BYTES = 200_000
_RESERVATION_PREFIX = "weekly_ai_resv_"
_RELEASE_IF_OWNED_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
""".strip()


class WeeklyAnalyticsConfigurationError(RuntimeError):
    """Raised when an execution prerequisite is missing or ambiguous."""


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _weekly_period(now: datetime | None) -> tuple[datetime, datetime, str]:
    operation_now = _utc(now)
    days_since_sunday = (operation_now.weekday() + 1) % 7
    period_end = (operation_now - timedelta(days=days_since_sunday)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    period_start = period_end - timedelta(days=7)
    period_key = f"{period_start:%Y%m%d}-{period_end:%Y%m%d}"
    return period_start, period_end, period_key


def _bounded_integer(
    raw_value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    code: str,
) -> int:
    value = default if raw_value is None else raw_value
    if isinstance(value, bool):
        raise WeeklyAnalyticsConfigurationError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WeeklyAnalyticsConfigurationError(code) from exc
    if parsed < minimum or parsed > maximum:
        raise WeeklyAnalyticsConfigurationError(code)
    return parsed


def _reservation_redis_url(app: Any) -> str:
    raw_url = str(
        app.config.get("WEEKLY_ANALYTICS_RESERVATION_REDIS_URL")
        or app.config.get("RATELIMIT_STORAGE_URI")
        or ""
    ).strip()
    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise WeeklyAnalyticsConfigurationError(
            "weekly_analytics_reservation_store_invalid"
        ) from exc
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise WeeklyAnalyticsConfigurationError(
            "weekly_analytics_reservation_store_invalid"
        )
    return raw_url


def _build_redis_client(app: Any):
    from redis import Redis

    return Redis.from_url(
        _reservation_redis_url(app),
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def _reservation_key(period_key: str, tenant_id: int) -> str:
    digest = hashlib.sha256(
        f"{period_key}:{int(tenant_id)}".encode("utf-8")
    ).hexdigest()
    return f"chatboc:weekly-analytics:v1:{digest}"


def _try_acquire_tenant_reservation(
    redis_client: Any,
    *,
    key: str,
    token: str,
    ttl_seconds: int,
) -> bool:
    try:
        acquired = redis_client.set(
            key,
            token,
            nx=True,
            ex=ttl_seconds,
        )
    except Exception as exc:
        raise WeeklyAnalyticsConfigurationError(
            "weekly_analytics_reservation_store_unavailable"
        ) from exc
    return acquired is True or acquired in {"OK", b"OK"}


def _release_tenant_reservation(
    redis_client: Any,
    *,
    key: str,
    token: str,
) -> None:
    try:
        redis_client.eval(_RELEASE_IF_OWNED_SCRIPT, 1, key, token)
    except Exception as exc:
        logger.error(
            "Weekly analytics reservation release failed; error_type=%s",
            type(exc).__name__,
        )


def _reservation_event_type(period_end: datetime) -> str:
    return f"{_RESERVATION_PREFIX}{period_end:%Y%m%d}"


def _candidate_tenants(*, event_type: str, limit: int) -> list[TenantProfile]:
    reserved_tenants = select(AnalyticsEvent.tenant_id).where(
        AnalyticsEvent.event_type == event_type
    )
    statement = (
        select(TenantProfile)
        .where(
            TenantProfile.is_active.is_(True),
            TenantProfile.tipo.in_(_SUPPORTED_TENANT_TYPES),
            TenantProfile.id.notin_(reserved_tenants),
        )
        .order_by(TenantProfile.id.asc())
        .limit(limit + 1)
    )
    return list(db.session.execute(statement).scalars())


def _unresolved_reservation_count(*, event_type: str) -> int:
    status = AnalyticsEvent.payload["status"].as_string()
    statement = select(func.count(AnalyticsEvent.id)).where(
        AnalyticsEvent.event_type == event_type,
        or_(status.is_(None), status != "completed"),
    )
    return int(db.session.execute(statement).scalar_one() or 0)


def _persist_provider_reservation(
    *,
    tenant_id: int,
    tenant_type: str,
    event_type: str,
    period_key: str,
    period_start: datetime,
    period_end: datetime,
) -> AnalyticsEvent | None:
    if db.engine.dialect.name == "postgresql":
        lock_digest = hashlib.sha256(
            f"{event_type}:{tenant_id}".encode("utf-8")
        ).digest()
        lock_id = int.from_bytes(lock_digest[:8], byteorder="big", signed=True)
        advisory_acquired = db.session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        ).scalar_one()
        if advisory_acquired is not True:
            db.session.rollback()
            return None
    current_tenant_type = db.session.execute(
        select(TenantProfile.tipo).where(
            TenantProfile.id == tenant_id,
            TenantProfile.is_active.is_(True),
            TenantProfile.tipo == tenant_type,
        )
    ).scalar_one_or_none()
    if current_tenant_type is None:
        db.session.rollback()
        return None
    existing = db.session.execute(
        select(AnalyticsEvent.id)
        .where(
            AnalyticsEvent.tenant_id == tenant_id,
            AnalyticsEvent.event_type == event_type,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        db.session.rollback()
        return None

    reservation = AnalyticsEvent(
        tenant_id=tenant_id,
        target_type=tenant_type,
        channel="system",
        event_type=event_type,
        payload={
            "contract_version": WEEKLY_ANALYTICS_RESERVATION_CONTRACT_VERSION,
            "period_key": period_key,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "report_type": f"consultant_{tenant_type}",
            "status": "provider_attempt_reserved",
        },
    )
    db.session.add(reservation)
    db.session.commit()
    return reservation


def _safe_provider_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise RuntimeError("weekly_analytics_provider_contract_invalid")
    summary = report.get("summary")
    tone = report.get("tone")
    opportunities = report.get("opportunities")
    threats = report.get("threats")
    if (
        not isinstance(summary, str)
        or len(summary) > 8_000
        or not isinstance(tone, str)
        or len(tone) > 200
        or not isinstance(opportunities, list)
        or not isinstance(threats, list)
        or len(opportunities) > 10
        or len(threats) > 10
    ):
        raise RuntimeError("weekly_analytics_provider_contract_invalid")
    if any(
        not isinstance(item, str) or len(item) > 2_000
        for item in [*opportunities, *threats]
    ):
        raise RuntimeError("weekly_analytics_provider_contract_invalid")
    if tone.strip().lower() == "error":
        raise RuntimeError("weekly_analytics_provider_report_unavailable")
    return {
        "summary": summary,
        "opportunities": list(opportunities),
        "threats": list(threats),
        "tone": tone,
    }


def _aggregate_tenant_summary(
    *,
    tenant_id: int,
    tenant_type: str,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Any]:
    query_end = period_end - timedelta(microseconds=1)
    summary = analytics_service.get_summary(
        tenant_id=tenant_id,
        start_date=period_start,
        end_date=query_end,
        context=tenant_type,
    )
    if tenant_type == "pyme":
        commerce = analytics_service.get_commerce_analytics(
            tenant_id=tenant_id,
            start_date=period_start,
            end_date=query_end,
            product_sample_limit=500,
        )
        summary.update(commerce)
    summary["report_window"] = {
        "start": period_start.isoformat(),
        "end": period_end.isoformat(),
        "timezone": "UTC",
    }
    encoded = json.dumps(
        summary,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_PROVIDER_INPUT_BYTES:
        raise RuntimeError("weekly_analytics_provider_input_too_large")
    return summary


def _mark_provider_uncertain(reservation: AnalyticsEvent) -> None:
    try:
        payload = dict(reservation.payload or {})
        payload["status"] = "provider_attempt_uncertain"
        reservation.payload = payload
        db.session.commit()
    except Exception:
        db.session.rollback()


def _persist_completed_report(
    *,
    reservation: AnalyticsEvent,
    tenant_id: int,
    tenant_type: str,
    report: dict[str, Any],
) -> None:
    reservation_payload = dict(reservation.payload or {})
    report_event_type = f"weekly_ai_report_consultant_{tenant_type}"
    cache_event = AnalyticsEvent(
        tenant_id=tenant_id,
        target_type=tenant_type,
        channel="system",
        event_type=report_event_type,
        payload={
            "contract_version": WEEKLY_ANALYTICS_CACHE_CONTRACT_VERSION,
            "source": "scheduled_weekly",
            "period_key": reservation_payload.get("period_key"),
            "period_start": reservation_payload.get("period_start"),
            "period_end": reservation_payload.get("period_end"),
            "report_type": f"consultant_{tenant_type}",
            "report": report,
        },
    )
    reservation_payload["status"] = "completed"
    reservation_payload["report_event_type"] = report_event_type
    reservation.payload = reservation_payload
    db.session.add(cache_event)
    db.session.commit()


def _empty_report(
    *,
    status: str,
    batch_limit: int,
    ok: bool = False,
) -> dict[str, Any]:
    return {
        "contract_version": WEEKLY_ANALYTICS_RUN_CONTRACT_VERSION,
        "ok": ok,
        "status": status,
        "batch_limit": batch_limit,
        "selected": 0,
        "provider_attempts": 0,
        "reports_generated": 0,
        "contended": 0,
        "failed_before_provider": 0,
        "provider_uncertain": 0,
        "reservation_failures": 0,
        "unresolved_reservations": 0,
        "has_more": False,
    }


def run_weekly_analytics_batch(
    app: Any,
    *,
    now: datetime | None = None,
    max_tenants: Any = None,
    redis_client: Any = None,
    report_generator: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate one bounded weekly batch with at-most-once provider attempts."""

    with app.app_context():
        try:
            batch_limit = _bounded_integer(
                max_tenants
                if max_tenants is not None
                else app.config.get("WEEKLY_ANALYTICS_MAX_TENANTS_PER_RUN"),
                default=DEFAULT_MAX_TENANTS_PER_RUN,
                minimum=1,
                maximum=MAX_TENANTS_PER_RUN,
                code="weekly_analytics_batch_limit_invalid",
            )
            ttl_seconds = _bounded_integer(
                app.config.get("WEEKLY_ANALYTICS_RESERVATION_TTL_SECONDS"),
                default=DEFAULT_RESERVATION_TTL_SECONDS,
                minimum=MIN_RESERVATION_TTL_SECONDS,
                maximum=MAX_RESERVATION_TTL_SECONDS,
                code="weekly_analytics_reservation_ttl_invalid",
            )
            reservation_store = (
                redis_client if redis_client is not None else _build_redis_client(app)
            )
            if report_generator is None:
                from services.openai_bridge import generate_analytics_report

                report_generator = generate_analytics_report
        except Exception as exc:
            logger.error(
                "Weekly analytics configuration failed; error_type=%s",
                type(exc).__name__,
            )
            return _empty_report(
                status="configuration_unavailable",
                batch_limit=0,
            )

        period_start, period_end, period_key = _weekly_period(now)
        event_type = _reservation_event_type(period_end)
        try:
            candidates = _candidate_tenants(
                event_type=event_type,
                limit=batch_limit,
            )
        except Exception as exc:
            db.session.rollback()
            logger.error(
                "Weekly analytics candidate selection failed; error_type=%s",
                type(exc).__name__,
            )
            return _empty_report(
                status="database_unavailable",
                batch_limit=batch_limit,
            )

        has_more = len(candidates) > batch_limit
        selected_candidates = candidates[:batch_limit]
        counters = {
            "provider_attempts": 0,
            "reports_generated": 0,
            "contended": 0,
            "failed_before_provider": 0,
            "provider_uncertain": 0,
            "reservation_failures": 0,
        }

        for tenant in selected_candidates:
            tenant_id = int(tenant.id)
            tenant_type = str(tenant.tipo).strip().lower()
            token = secrets.token_hex(24)
            reservation_key = _reservation_key(period_key, tenant_id)
            try:
                acquired = _try_acquire_tenant_reservation(
                    reservation_store,
                    key=reservation_key,
                    token=token,
                    ttl_seconds=ttl_seconds,
                )
            except WeeklyAnalyticsConfigurationError as exc:
                logger.error(
                    "Weekly analytics reservation unavailable; error_type=%s",
                    type(exc).__name__,
                )
                counters["reservation_failures"] += 1
                break
            if not acquired:
                counters["contended"] += 1
                continue

            try:
                summary = _aggregate_tenant_summary(
                    tenant_id=tenant_id,
                    tenant_type=tenant_type,
                    period_start=period_start,
                    period_end=period_end,
                )
            except Exception as exc:
                _release_tenant_reservation(
                    reservation_store,
                    key=reservation_key,
                    token=token,
                )
                logger.error(
                    "Weekly analytics aggregation failed; error_type=%s",
                    type(exc).__name__,
                )
                counters["failed_before_provider"] += 1
                continue

            # Persist the durable fence only once all local aggregation has
            # succeeded. This keeps the conservative at-most-once boundary
            # immediately adjacent to the paid provider call.
            try:
                reservation = _persist_provider_reservation(
                    tenant_id=tenant_id,
                    tenant_type=tenant_type,
                    event_type=event_type,
                    period_key=period_key,
                    period_start=period_start,
                    period_end=period_end,
                )
            except Exception as exc:
                db.session.rollback()
                _release_tenant_reservation(
                    reservation_store,
                    key=reservation_key,
                    token=token,
                )
                logger.error(
                    "Weekly analytics reservation persistence failed; error_type=%s",
                    type(exc).__name__,
                )
                counters["reservation_failures"] += 1
                continue
            if reservation is None:
                _release_tenant_reservation(
                    reservation_store,
                    key=reservation_key,
                    token=token,
                )
                counters["contended"] += 1
                continue

            counters["provider_attempts"] += 1
            try:
                generated = report_generator(summary, tenant_type)
                safe_report = _safe_provider_report(generated)
            except Exception as exc:
                _mark_provider_uncertain(reservation)
                logger.error(
                    "Weekly analytics provider attempt uncertain; error_type=%s",
                    type(exc).__name__,
                )
                counters["provider_uncertain"] += 1
                continue

            try:
                _persist_completed_report(
                    reservation=reservation,
                    tenant_id=tenant_id,
                    tenant_type=tenant_type,
                    report=safe_report,
                )
            except Exception as exc:
                db.session.rollback()
                _mark_provider_uncertain(reservation)
                logger.error(
                    "Weekly analytics cache persistence failed; error_type=%s",
                    type(exc).__name__,
                )
                counters["provider_uncertain"] += 1
                continue
            counters["reports_generated"] += 1

        try:
            unresolved_reservations = _unresolved_reservation_count(
                event_type=event_type
            )
        except Exception as exc:
            db.session.rollback()
            logger.error(
                "Weekly analytics reservation state failed; error_type=%s",
                type(exc).__name__,
            )
            counters["reservation_failures"] += 1
            unresolved_reservations = 1

        degraded = unresolved_reservations > 0 or any(
            counters[key] > 0
            for key in {
                "failed_before_provider",
                "provider_uncertain",
                "reservation_failures",
            }
        )
        if degraded:
            status = "degraded"
        elif has_more:
            status = "batch_limit_reached"
        elif counters["contended"] and not counters["reports_generated"]:
            status = "contended"
        else:
            status = "completed"
        return {
            "contract_version": WEEKLY_ANALYTICS_RUN_CONTRACT_VERSION,
            "ok": status in {"completed", "batch_limit_reached"},
            "status": status,
            "batch_limit": batch_limit,
            "selected": len(selected_candidates),
            **counters,
            "unresolved_reservations": unresolved_reservations,
            "has_more": has_more,
        }


def run_weekly_analytics_drain(
    app: Any,
    *,
    now: datetime | None = None,
    max_batches: Any = None,
    max_tenants: Any = None,
    redis_client: Any = None,
    report_generator: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Drain multiple safe batches for long-lived cron runtimes such as Render."""

    with app.app_context():
        try:
            batch_budget = _bounded_integer(
                max_batches
                if max_batches is not None
                else app.config.get("WEEKLY_ANALYTICS_MAX_BATCHES_PER_DRAIN"),
                default=DEFAULT_MAX_BATCHES_PER_DRAIN,
                minimum=1,
                maximum=MAX_BATCHES_PER_DRAIN,
                code="weekly_analytics_drain_batch_budget_invalid",
            )
        except WeeklyAnalyticsConfigurationError:
            return {
                "contract_version": WEEKLY_ANALYTICS_DRAIN_CONTRACT_VERSION,
                "ok": False,
                "status": "configuration_unavailable",
                "batch_budget": 0,
                "batches_run": 0,
                "selected": 0,
                "provider_attempts": 0,
                "reports_generated": 0,
                "contended": 0,
                "failed_before_provider": 0,
                "provider_uncertain": 0,
                "reservation_failures": 0,
                "unresolved_reservations": 0,
                "has_more": False,
            }

    operation_now = _utc(now)
    counters = {
        "selected": 0,
        "provider_attempts": 0,
        "reports_generated": 0,
        "contended": 0,
        "failed_before_provider": 0,
        "provider_uncertain": 0,
        "reservation_failures": 0,
    }
    batches_run = 0
    unresolved_reservations = 0
    has_more = False
    status = "completed"

    for _ in range(batch_budget):
        batch = run_weekly_analytics_batch(
            app,
            now=operation_now,
            max_tenants=max_tenants,
            redis_client=redis_client,
            report_generator=report_generator,
        )
        batches_run += 1
        for key in counters:
            counters[key] += int(batch.get(key, 0) or 0)
        unresolved_reservations = int(
            batch.get("unresolved_reservations", 0) or 0
        )
        has_more = batch.get("has_more") is True
        status = str(batch.get("status") or "failed")

        # A degraded batch may still have fenced provider attempts and valid
        # reports. Continue draining later tenant IDs whenever durable progress
        # was made; stop on a zero-progress failure to avoid a tight retry loop.
        if has_more and int(batch.get("provider_attempts", 0) or 0) > 0:
            continue
        break
    else:
        if has_more:
            status = "drain_limit_reached"

    return {
        "contract_version": WEEKLY_ANALYTICS_DRAIN_CONTRACT_VERSION,
        "ok": status == "completed",
        "status": status,
        "batch_budget": batch_budget,
        "batches_run": batches_run,
        **counters,
        "unresolved_reservations": unresolved_reservations,
        "has_more": has_more,
    }


__all__ = [
    "MAX_BATCHES_PER_DRAIN",
    "MAX_TENANTS_PER_RUN",
    "WEEKLY_ANALYTICS_CACHE_CONTRACT_VERSION",
    "WEEKLY_ANALYTICS_DRAIN_CONTRACT_VERSION",
    "WEEKLY_ANALYTICS_RUN_CONTRACT_VERSION",
    "WeeklyAnalyticsConfigurationError",
    "run_weekly_analytics_batch",
    "run_weekly_analytics_drain",
]
