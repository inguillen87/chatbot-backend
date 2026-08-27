"""Bounded reconciliation for durable outboxes on scheduled runtimes.

Vercel Cron is a wake-up mechanism, not a permanent process. Each invocation
therefore holds one PostgreSQL transaction advisory lease and drains small,
leased batches from the existing authoritative database outboxes. The worker
implementations retain effect-level idempotency and fencing; this coordinator
only adds invocation exclusivity, a cooperative time budget and payload-free
operational metrics.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from models import db
from services.domain_effect_worker import run_domain_effect_worker
from services.outbox_execution_budget import (
    DEFAULT_CLEANUP_RESERVE_SECONDS,
    activate_outbox_execution_budget,
    install_outbox_database_timeout_hook,
    outbox_execution_budget_active,
)
from services.survey_response_effect_worker import (
    run_survey_response_effect_worker,
)
from services.whatsapp_inbound_worker import run_whatsapp_durable_worker


logger = logging.getLogger(__name__)

OUTBOX_RECONCILIATION_CONTRACT_VERSION = "outbox.reconciliation.v2"

_ADVISORY_LOCK_ID = int.from_bytes(
    hashlib.sha256(
        # Keep the lock identity stable across coordinator contract upgrades
        # and rollbacks. Mixed releases must still exclude one another.
        b"chatboc:vercel-outbox-reconciliation",
    ).digest()[:8],
    byteorder="big",
    signed=True,
)

_COUNTER_FIELDS = (
    "cycles",
    "claimed",
    "processed",
    "succeeded",
    "skipped",
    "inbound_processed",
    "inbound_completed",
    "outbound_processed",
    "outbound_accepted",
    "retry_wait",
    "unknown",
    "dead",
    "recovered_unknown",
    "recovered_retry_wait",
    "recovered_dead",
    "fenced",
    "cycle_failures",
)


class OutboxReconciliationConfigurationError(RuntimeError):
    """Raised when a cron bound or its exclusive lease is unsafe."""


class OutboxReconciliationReportError(RuntimeError):
    """Raised when a worker returns an unexpected operational contract."""


@dataclass(frozen=True)
class ReconciliationLimits:
    time_budget_seconds: float
    max_cycles: int
    whatsapp_inbound_batch_size: int
    whatsapp_outbound_batch_size: int
    domain_effect_batch_size: int
    survey_effect_batch_size: int

    def to_dict(self) -> dict[str, int | float]:
        return {
            "time_budget_seconds": self.time_budget_seconds,
            "max_cycles": self.max_cycles,
            "whatsapp_inbound_batch_size": self.whatsapp_inbound_batch_size,
            "whatsapp_outbound_batch_size": self.whatsapp_outbound_batch_size,
            "domain_effect_batch_size": self.domain_effect_batch_size,
            "survey_effect_batch_size": self.survey_effect_batch_size,
        }


@dataclass(frozen=True)
class _PipelineSpec:
    name: str
    batch_limit: int | dict[str, int]
    lease_seconds: int
    contracts: frozenset[str]
    runner: Callable[[], Any]


def _bounded_integer(
    config: Mapping[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = config.get(key, default)
    if isinstance(raw_value, bool):
        raise OutboxReconciliationConfigurationError(f"{key.lower()}_invalid")
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str) and raw_value.strip().lstrip("+-").isdigit():
        value = int(raw_value.strip())
    else:
        raise OutboxReconciliationConfigurationError(f"{key.lower()}_invalid")
    if value < minimum or value > maximum:
        raise OutboxReconciliationConfigurationError(f"{key.lower()}_invalid")
    return value


def _bounded_float(
    config: Mapping[str, Any],
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = config.get(key, default)
    if isinstance(raw_value, bool):
        raise OutboxReconciliationConfigurationError(f"{key.lower()}_invalid")
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutboxReconciliationConfigurationError(
            f"{key.lower()}_invalid"
        ) from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise OutboxReconciliationConfigurationError(f"{key.lower()}_invalid")
    return value


def _configured_limits(app: Any) -> ReconciliationLimits:
    config = app.config
    return ReconciliationLimits(
        time_budget_seconds=_bounded_float(
            config,
            "VERCEL_OUTBOX_CRON_TIME_BUDGET_SECONDS",
            default=45.0,
            minimum=5.0,
            maximum=55.0,
        ),
        max_cycles=_bounded_integer(
            config,
            "VERCEL_OUTBOX_CRON_MAX_CYCLES",
            default=4,
            minimum=1,
            maximum=20,
        ),
        whatsapp_inbound_batch_size=_bounded_integer(
            config,
            "VERCEL_OUTBOX_CRON_WHATSAPP_INBOUND_BATCH_SIZE",
            default=1,
            minimum=1,
            maximum=10,
        ),
        whatsapp_outbound_batch_size=_bounded_integer(
            config,
            "VERCEL_OUTBOX_CRON_WHATSAPP_OUTBOUND_BATCH_SIZE",
            default=2,
            minimum=1,
            maximum=10,
        ),
        domain_effect_batch_size=_bounded_integer(
            config,
            "VERCEL_OUTBOX_CRON_DOMAIN_EFFECT_BATCH_SIZE",
            default=10,
            minimum=1,
            maximum=50,
        ),
        survey_effect_batch_size=_bounded_integer(
            config,
            "VERCEL_OUTBOX_CRON_SURVEY_EFFECT_BATCH_SIZE",
            default=25,
            minimum=1,
            maximum=100,
        ),
    )


def _configured_lease_seconds(
    config: Mapping[str, Any],
    key: str,
    *,
    default: int,
) -> int:
    return _bounded_integer(
        config,
        key,
        default=default,
        minimum=30,
        maximum=3600,
    )


@contextmanager
def _exclusive_reconciliation_lease(app: Any) -> Iterator[bool]:
    """Hold one PgBouncer-safe transaction advisory lease for this drain.

    Neon pooled connections may use transaction pooling, so a session advisory
    lock is not safe. Keeping one explicit transaction open pins the database
    connection for ``pg_try_advisory_xact_lock`` and releases the lock on
    rollback, connection loss or process termination.
    """

    connection = None
    transaction = None
    with app.app_context():
        try:
            engine = db.engine
            if engine.dialect.name != "postgresql":
                raise OutboxReconciliationConfigurationError(
                    "outbox_reconciliation_requires_postgresql"
                )
            if outbox_execution_budget_active():
                install_outbox_database_timeout_hook(engine)
            connection = engine.connect()
            transaction = connection.begin()
            acquired = connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": _ADVISORY_LOCK_ID},
            ).scalar_one()
            if not isinstance(acquired, bool):
                raise OutboxReconciliationConfigurationError(
                    "outbox_reconciliation_lease_invalid"
                )
            yield acquired
        finally:
            if transaction is not None and transaction.is_active:
                try:
                    transaction.rollback()
                except Exception as exc:
                    logger.error(
                        "[OUTBOX_RECONCILIATION] lease_release_failed "
                        "error_type=%s",
                        type(exc).__name__,
                    )
                    if connection is not None:
                        connection.invalidate()
            if connection is not None:
                connection.close()


def _payload_safe_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the cron response useful without tenant/effect/provider payloads."""

    summary: dict[str, Any] = {}
    for field in ("contract_version", "status", "mode", *_COUNTER_FIELDS):
        if field not in report:
            continue
        value = report[field]
        if field in _COUNTER_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise OutboxReconciliationReportError(
                    "outbox_reconciliation_counter_invalid"
                )
            if value < 0:
                raise OutboxReconciliationReportError(
                    "outbox_reconciliation_counter_invalid"
                )
        elif not isinstance(value, str):
            raise OutboxReconciliationReportError(
                "outbox_reconciliation_report_invalid"
            )
        summary[field] = value
    return summary


def _reconciliation_runners(
    app: Any,
    limits: ReconciliationLimits,
    *,
    deadline_monotonic: float,
    clock: Callable[[], float],
) -> tuple[_PipelineSpec, ...]:
    config = app.config
    whatsapp_lease = _configured_lease_seconds(
        config,
        "WHATSAPP_INBOUND_LEASE_SECONDS",
        default=180,
    )
    domain_lease = _configured_lease_seconds(
        config,
        "DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS",
        default=180,
    )
    survey_lease = _configured_lease_seconds(
        config,
        "SURVEY_RESPONSE_EFFECT_LEASE_SECONDS",
        default=120,
    )
    return (
        _PipelineSpec(
            name="whatsapp",
            batch_limit={
                "inbound": limits.whatsapp_inbound_batch_size,
                "outbound": limits.whatsapp_outbound_batch_size,
            },
            lease_seconds=whatsapp_lease,
            contracts=frozenset(
                {
                    "whatsapp.durable_worker_run.v1",
                    "whatsapp.durable_worker_standby.v1",
                }
            ),
            runner=lambda: run_whatsapp_durable_worker(
                app,
                once=True,
                standby_when_legacy=True,
                inbound_limit=limits.whatsapp_inbound_batch_size,
                outbound_limit=limits.whatsapp_outbound_batch_size,
                deadline_monotonic=deadline_monotonic,
                clock=clock,
            ),
        ),
        _PipelineSpec(
            name="domain_effects",
            batch_limit=limits.domain_effect_batch_size,
            lease_seconds=domain_lease,
            contracts=frozenset({"domain.effect_worker_run.v1"}),
            runner=lambda: run_domain_effect_worker(
                app,
                once=True,
                batch_limit=limits.domain_effect_batch_size,
                deadline_monotonic=deadline_monotonic,
                clock=clock,
            ),
        ),
        _PipelineSpec(
            name="survey_effects",
            batch_limit=limits.survey_effect_batch_size,
            lease_seconds=survey_lease,
            contracts=frozenset({"surveys.response_effect_worker_run.v1"}),
            runner=lambda: run_survey_response_effect_worker(
                app,
                once=True,
                batch_limit=limits.survey_effect_batch_size,
                deadline_monotonic=deadline_monotonic,
                clock=clock,
                require_current_schema=True,
                require_shared_realtime=True,
            ),
        ),
    )


def _empty_component(
    spec: _PipelineSpec,
    *,
    max_cycles: int,
) -> dict[str, Any]:
    if isinstance(spec.batch_limit, dict):
        max_total: int | dict[str, int] = {
            key: value * max_cycles for key, value in spec.batch_limit.items()
        }
    else:
        max_total = spec.batch_limit * max_cycles
    return {
        "status": "not_run",
        "attempts": 0,
        "per_cycle_batch_limit": spec.batch_limit,
        "max_total_per_invocation": max_total,
        "lease_seconds": spec.lease_seconds,
        **{field: 0 for field in _COUNTER_FIELDS},
    }


def _required_report_fields(contract_version: str) -> frozenset[str]:
    if contract_version == "whatsapp.durable_worker_standby.v1":
        return frozenset(
            {
                "contract_version",
                "status",
                "mode",
                "cycles",
                "processed",
                "inbound_processed",
                "inbound_completed",
                "outbound_processed",
                "outbound_accepted",
                "retry_wait",
                "unknown",
                "dead",
                "cycle_failures",
            }
        )
    if contract_version == "whatsapp.durable_worker_run.v1":
        return _required_report_fields("whatsapp.durable_worker_standby.v1")
    if contract_version == "domain.effect_worker_run.v1":
        return frozenset(
            {
                "contract_version",
                "cycles",
                "processed",
                "succeeded",
                "skipped",
                "retry_wait",
                "unknown",
                "dead",
                "recovered_unknown",
                "recovered_retry_wait",
                "recovered_dead",
                "cycle_failures",
            }
        )
    if contract_version == "surveys.response_effect_worker_run.v1":
        return frozenset(
            {
                "contract_version",
                "cycles",
                "claimed",
                "processed",
                "succeeded",
                "skipped",
                "retry_wait",
                "dead",
                "fenced",
                "cycle_failures",
            }
        )
    return frozenset()


def _safe_report(raw_report: Any, *, spec: _PipelineSpec) -> dict[str, Any]:
    if not isinstance(raw_report, Mapping):
        raise OutboxReconciliationReportError(
            "outbox_reconciliation_report_invalid"
        )
    report = _payload_safe_summary(raw_report)
    contract_version = str(report.get("contract_version") or "")
    if contract_version not in spec.contracts:
        raise OutboxReconciliationReportError(
            "outbox_reconciliation_contract_invalid"
        )
    if not _required_report_fields(contract_version).issubset(report):
        raise OutboxReconciliationReportError(
            "outbox_reconciliation_report_incomplete"
        )

    cycles = int(report["cycles"])
    cycle_failures = int(report["cycle_failures"])
    processed = int(report["processed"])
    if contract_version == "whatsapp.durable_worker_standby.v1":
        if (
            report.get("status") != "standby"
            or report.get("mode") != "legacy"
            or cycles != 0
            or any(int(report.get(field) or 0) for field in _COUNTER_FIELDS)
        ):
            raise OutboxReconciliationReportError(
                "outbox_reconciliation_standby_invalid"
            )
    elif contract_version == "whatsapp.durable_worker_run.v1":
        limits = spec.batch_limit
        if not isinstance(limits, dict):
            raise OutboxReconciliationReportError(
                "outbox_reconciliation_batch_contract_invalid"
            )
        inbound_processed = int(report["inbound_processed"])
        outbound_processed = int(report["outbound_processed"])
        if (
            report.get("status") != "completed"
            or report.get("mode") != "queue"
            or cycles != 1
            or cycle_failures > 1
            or inbound_processed > limits["inbound"]
            or outbound_processed > limits["outbound"]
            or processed != inbound_processed + outbound_processed
            or int(report["inbound_completed"]) > inbound_processed
            or int(report["outbound_accepted"]) > outbound_processed
            or (
                int(report["inbound_completed"])
                + int(report["outbound_accepted"])
                + sum(
                    int(report[field])
                    for field in ("retry_wait", "unknown", "dead")
                )
            )
            != processed
            or sum(
                int(report[field])
                for field in ("retry_wait", "unknown", "dead")
            )
            > processed
        ):
            raise OutboxReconciliationReportError(
                "outbox_reconciliation_whatsapp_report_invalid"
            )
    elif contract_version == "domain.effect_worker_run.v1":
        recovered_count = sum(
            int(report[field])
            for field in (
                "recovered_unknown",
                "recovered_retry_wait",
                "recovered_dead",
            )
        )
        if (
            cycles != 1
            or cycle_failures > 1
            or not isinstance(spec.batch_limit, int)
            or processed > spec.batch_limit
            or processed + recovered_count > spec.batch_limit
            or sum(
                int(report[field])
                for field in (
                    "succeeded",
                    "skipped",
                    "retry_wait",
                    "unknown",
                    "dead",
                )
            )
            != processed
        ):
            raise OutboxReconciliationReportError(
                "outbox_reconciliation_domain_report_invalid"
            )
    elif contract_version == "surveys.response_effect_worker_run.v1":
        claimed = int(report["claimed"])
        fenced = int(report["fenced"])
        if (
            cycles != 1
            or cycle_failures > 1
            or not isinstance(spec.batch_limit, int)
            or claimed > spec.batch_limit
            or processed > claimed
            or processed + fenced != claimed
            or int(report["retry_wait"]) + int(report["dead"]) > processed
            or sum(
                int(report[field])
                for field in ("succeeded", "skipped", "retry_wait", "dead")
            )
            != processed
        ):
            raise OutboxReconciliationReportError(
                "outbox_reconciliation_survey_report_invalid"
            )
    return report


def _merge_component_report(
    component: dict[str, Any],
    report: Mapping[str, Any],
) -> int:
    component["attempts"] += 1
    for field in _COUNTER_FIELDS:
        component[field] += int(report.get(field) or 0)
    processed = int(report.get("processed") or 0)
    if report.get("status") == "standby":
        component["status"] = "standby"
    elif (
        int(report.get("unknown") or 0)
        or int(report.get("dead") or 0)
        or int(report.get("recovered_unknown") or 0)
        or int(report.get("recovered_dead") or 0)
    ):
        component["status"] = "attention_required"
    elif int(report.get("fenced") or 0):
        component["status"] = "fenced"
    elif (
        int(report.get("retry_wait") or 0)
        or int(report.get("recovered_retry_wait") or 0)
    ):
        component["status"] = "retry_wait"
    elif processed > 0:
        component["status"] = "progress"
    else:
        component["status"] = "idle"
    component["last_report"] = dict(report)
    return processed


def _base_payload(
    *,
    limits: ReconciliationLimits | None,
    status: str,
    ok: bool,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "contract_version": OUTBOX_RECONCILIATION_CONTRACT_VERSION,
        "ok": ok,
        "status": status,
        "idempotency": "database_outbox_leases_and_fencing",
        "exclusive_lease": "postgresql_transaction_advisory_lock",
        "elapsed_ms": max(0, int(elapsed_ms)),
        "limits": limits.to_dict() if limits is not None else {},
    }


def run_outbox_reconciliation(
    app: Any,
    *,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Drain every durable outbox within one exclusive, bounded cron tick."""

    started_at = clock()
    try:
        limits = _configured_limits(app)
        deadline = started_at + limits.time_budget_seconds
        specs = _reconciliation_runners(
            app,
            limits,
            deadline_monotonic=deadline,
            clock=clock,
        )
    except Exception as exc:
        logger.error(
            "[OUTBOX_RECONCILIATION] configuration_failed error_type=%s",
            type(exc).__name__,
        )
        payload = _base_payload(
            limits=None,
            status="configuration_unavailable",
            ok=False,
            elapsed_ms=round((clock() - started_at) * 1000),
        )
        payload.update(
            component_count=0,
            failed_component_count=0,
            attention_component_count=0,
            cycles_run=0,
            has_more=False,
            components={},
        )
        return payload

    components = {
        spec.name: _empty_component(spec, max_cycles=limits.max_cycles)
        for spec in specs
    }
    failed_components: set[str] = set()
    attention_components: set[str] = set()
    cycles_run = 0
    status = "completed"
    has_more = False

    try:
        with activate_outbox_execution_budget(
            deadline_monotonic=deadline,
            clock=clock,
            cleanup_reserve_seconds=min(
                DEFAULT_CLEANUP_RESERVE_SECONDS,
                max(1.0, limits.time_budget_seconds / 3.0),
            ),
        ):
            with _exclusive_reconciliation_lease(app) as lease_acquired:
                if not lease_acquired:
                    payload = _base_payload(
                        limits=limits,
                        status="contended",
                        ok=True,
                        elapsed_ms=round((clock() - started_at) * 1000),
                    )
                    payload.update(
                        component_count=len(components),
                        failed_component_count=0,
                        attention_component_count=0,
                        cycles_run=0,
                        has_more=True,
                        components=components,
                    )
                    return payload

                # Change the first pipeline every UTC minute. Duplicate
                # deliveries in one minute still contend on the DB lease,
                # while sustained backlog cannot starve the same later
                # pipeline every invocation.
                invocation_offset = int(wall_clock() // 60) % len(specs)
                for cycle_index in range(limits.max_cycles):
                    if clock() >= deadline:
                        status = "time_budget_reached"
                        has_more = True
                        break

                    cycles_run += 1
                    cycle_processed = 0
                    cycle_failed = False
                    cycle_attention = False
                    offset = (invocation_offset + cycle_index) % len(specs)
                    ordered_specs = specs[offset:] + specs[:offset]
                    for spec_index, spec in enumerate(ordered_specs):
                        if clock() >= deadline:
                            status = "time_budget_reached"
                            has_more = True
                            for deferred in ordered_specs[spec_index:]:
                                deferred_component = components[deferred.name]
                                if deferred_component["attempts"] == 0:
                                    deferred_component["status"] = (
                                        "deferred_time_budget"
                                    )
                            break
                        component = components[spec.name]
                        attempt_recorded = False
                        try:
                            report = _safe_report(spec.runner(), spec=spec)
                            cycle_processed += _merge_component_report(
                                component,
                                report,
                            )
                            attempt_recorded = True
                            if int(report.get("cycle_failures") or 0) > 0:
                                cycle_failed = True
                                failed_components.add(spec.name)
                                component["status"] = "failed"
                                component["error_type"] = (
                                    "WorkerCycleFailure"
                                )
                                logger.error(
                                    "[OUTBOX_RECONCILIATION] component_failed "
                                    "component=%s error_type=WorkerCycleFailure",
                                    spec.name,
                                )
                            elif component["status"] == "attention_required":
                                cycle_attention = True
                                attention_components.add(spec.name)
                            elif int(report.get("fenced") or 0) > 0:
                                cycle_failed = True
                                failed_components.add(spec.name)
                                component["status"] = "fenced"
                                component["error_type"] = "WorkerFence"
                                logger.warning(
                                    "[OUTBOX_RECONCILIATION] component_fenced "
                                    "component=%s count=%s",
                                    spec.name,
                                    int(report["fenced"]),
                                )
                            elif (
                                int(report.get("retry_wait") or 0) > 0
                                or int(report.get("recovered_retry_wait") or 0) > 0
                            ):
                                cycle_failed = True
                                failed_components.add(spec.name)
                                component["status"] = "retry_wait"
                                component["error_type"] = "WorkerRetryWait"
                                logger.warning(
                                    "[OUTBOX_RECONCILIATION] component_retry_wait "
                                    "component=%s count=%s",
                                    spec.name,
                                    int(report.get("retry_wait") or 0)
                                    + int(report.get("recovered_retry_wait") or 0),
                                )
                        except Exception as exc:
                            cycle_failed = True
                            failed_components.add(spec.name)
                            if not attempt_recorded:
                                component["attempts"] += 1
                                component["cycle_failures"] += 1
                            component["status"] = "failed"
                            component["error_type"] = type(exc).__name__
                            logger.error(
                                "[OUTBOX_RECONCILIATION] component_failed "
                                "component=%s error_type=%s",
                                spec.name,
                                type(exc).__name__,
                            )

                    if cycle_failed:
                        status = "degraded"
                        has_more = True
                        break
                    if cycle_attention:
                        status = "degraded"
                        has_more = True
                        break
                    if status == "time_budget_reached":
                        break
                    if cycle_processed == 0:
                        status = "completed"
                        has_more = False
                        break
                    if clock() >= deadline:
                        status = "time_budget_reached"
                        has_more = True
                        break
                else:
                    status = "drain_limit_reached"
                    has_more = True
    except Exception as exc:
        logger.error(
            "[OUTBOX_RECONCILIATION] reconciliation_unavailable error_type=%s",
            type(exc).__name__,
        )
        status = "reconciliation_unavailable"
        has_more = True

    ok = status in {
        "completed",
        "contended",
        "drain_limit_reached",
        "time_budget_reached",
    }
    payload = _base_payload(
        limits=limits,
        status=status,
        ok=ok,
        elapsed_ms=round((clock() - started_at) * 1000),
    )
    payload.update(
        component_count=len(components),
        failed_component_count=len(failed_components),
        attention_component_count=len(attention_components),
        cycles_run=cycles_run,
        has_more=has_more,
        components=components,
    )
    return payload


__all__ = [
    "OUTBOX_RECONCILIATION_CONTRACT_VERSION",
    "OutboxReconciliationConfigurationError",
    "ReconciliationLimits",
    "run_outbox_reconciliation",
]
