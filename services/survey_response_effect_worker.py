"""Permanent DB poller for durable survey-response effects.

The database outbox is authoritative.  Celery can wake a bounded sweep, while
the dedicated process continuously discovers tenants with due work so a
broker outage or a missed inline dispatch cannot strand effects.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any, Callable, Optional

from flask import current_app, has_app_context
from sqlalchemy import func
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from celery_utils import celery_app
from models import SurveyResponseEffect, db
from services.survey_response_effects import (
    dispatch_survey_response_effects,
    list_due_survey_response_effect_tenant_ids,
)


logger = logging.getLogger(__name__)

SURVEY_RESPONSE_EFFECT_SWEEP_TASK_NAME = (
    "chatboc.survey_response_effect_outbox.dispatch"
)
SURVEY_RESPONSE_EFFECT_WORKER_BATCH_CONTRACT = (
    "surveys.response_effect_worker_batch.v1"
)
SURVEY_RESPONSE_EFFECT_WORKER_RUN_CONTRACT = (
    "surveys.response_effect_worker_run.v1"
)
SURVEY_RESPONSE_EFFECT_WORKER_HEALTH_CONTRACT = (
    "surveys.response_effect_worker_health.v1"
)

_ROUND_ROBIN_EXTENSION_KEY = "chatboc.survey_response_effect_worker.cursor"
_ROUND_ROBIN_LOCK = threading.Lock()
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class SurveyResponseEffectWorkerConfigurationError(RuntimeError):
    """Raised when a poller setting would make processing unsafe."""


def _repository_schema_heads() -> frozenset[str]:
    config = AlembicConfig(str(_REPOSITORY_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    return frozenset(str(head) for head in scripts.get_heads())


def _database_schema_heads() -> frozenset[str]:
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return frozenset(str(head) for head in context.get_current_heads())


def assert_survey_response_effect_worker_schema_current(
    *,
    required: bool = False,
) -> None:
    """Fail before polling when the database is not at this release's head."""

    if bool(current_app.config.get("TESTING")):
        return
    process_role = str(
        current_app.config.get("CHATBOC_PROCESS_ROLE")
        or os.getenv("CHATBOC_PROCESS_ROLE", "")
    ).strip().lower()
    if not required and process_role != "survey-effect-worker":
        return

    expected = _repository_schema_heads()
    current = _database_schema_heads()
    if not expected or current != expected:
        raise SurveyResponseEffectWorkerConfigurationError(
            "survey_response_effect_worker_schema_not_current"
        )


def _configured_int(
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(current_app.config.get(key, default))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurveyResponseEffectWorkerConfigurationError(
            f"{key.lower()}_invalid"
        ) from exc
    if value < minimum or value > maximum:
        raise SurveyResponseEffectWorkerConfigurationError(
            f"{key.lower()}_invalid"
        )
    return value


def _configured_float(
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(current_app.config.get(key, default))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SurveyResponseEffectWorkerConfigurationError(
            f"{key.lower()}_invalid"
        ) from exc
    if value < minimum or value > maximum:
        raise SurveyResponseEffectWorkerConfigurationError(
            f"{key.lower()}_invalid"
        )
    return value


def _configured_batch_size() -> int:
    return _configured_int(
        "SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE",
        default=50,
        minimum=1,
        maximum=500,
    )


def _configured_max_tenants() -> int:
    return _configured_int(
        "SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE",
        default=50,
        minimum=1,
        maximum=500,
    )


def _configured_lease_seconds() -> int:
    return _configured_int(
        "SURVEY_RESPONSE_EFFECT_LEASE_SECONDS",
        default=120,
        minimum=30,
        maximum=3600,
    )


def _configured_poll_seconds() -> float:
    return _configured_float(
        "SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS",
        default=0.5,
        minimum=0.05,
        maximum=60.0,
    )


def _select_fair_tenants(
    tenant_ids: tuple[int, ...],
    *,
    maximum: int,
) -> tuple[int, ...]:
    """Select a rotating tenant window without letting one backlog starve peers."""

    if not tenant_ids or maximum <= 0:
        return ()
    selected_count = min(len(tenant_ids), int(maximum))
    with _ROUND_ROBIN_LOCK:
        start = int(
            current_app.extensions.get(_ROUND_ROBIN_EXTENSION_KEY, 0) or 0
        ) % len(tenant_ids)
        rotated = tenant_ids[start:] + tenant_ids[:start]
        selected = rotated[:selected_count]
        current_app.extensions[_ROUND_ROBIN_EXTENSION_KEY] = (
            start + selected_count
        ) % len(tenant_ids)
    return selected


def dispatch_survey_response_effect_batch(
    *,
    limit: Optional[int] = None,
    deadline_monotonic: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    require_shared_realtime: bool = False,
) -> dict[str, Any]:
    """Discover tenants and process one bounded, fairly divided DB batch."""

    if not has_app_context():
        raise SurveyResponseEffectWorkerConfigurationError(
            "survey_response_effect_worker_app_context_required"
        )

    configured_limit = _configured_batch_size() if limit is None else int(limit)
    bounded_limit = max(1, min(configured_limit, 500))
    lease_seconds = _configured_lease_seconds()
    due_tenant_ids = list_due_survey_response_effect_tenant_ids()
    selected_tenants = _select_fair_tenants(
        due_tenant_ids,
        maximum=min(_configured_max_tenants(), bounded_limit),
    )

    totals = {
        "claimed": 0,
        "processed": 0,
        "succeeded": 0,
        "skipped": 0,
        "retry_wait": 0,
        "dead": 0,
        "fenced": 0,
    }
    per_tenant: list[dict[str, Any]] = []
    remaining = bounded_limit

    for tenant_index, tenant_id in enumerate(selected_tenants):
        if remaining <= 0:
            break
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            break
        tenants_remaining = len(selected_tenants) - tenant_index
        tenant_limit = max(
            1,
            (remaining + tenants_remaining - 1) // tenants_remaining,
        )
        dispatch_kwargs: dict[str, Any] = {}
        if deadline_monotonic is not None:
            dispatch_kwargs["should_continue"] = (
                lambda: clock() < deadline_monotonic
            )
        if require_shared_realtime:
            dispatch_kwargs["require_shared_realtime"] = True
        report = dispatch_survey_response_effects(
            tenant_id=tenant_id,
            limit=tenant_limit,
            lease_seconds=lease_seconds,
            **dispatch_kwargs,
        )
        safe_report = {
            key: int(report.get(key) or 0)
            for key in totals
        }
        safe_report["tenant_id"] = int(tenant_id)
        per_tenant.append(safe_report)
        for key in totals:
            totals[key] += safe_report[key]
        remaining -= safe_report["claimed"]

    return {
        "contract_version": SURVEY_RESPONSE_EFFECT_WORKER_BATCH_CONTRACT,
        "limit": bounded_limit,
        "lease_seconds": lease_seconds,
        **totals,
        "tenants": per_tenant,
    }


@celery_app.task(
    name=SURVEY_RESPONSE_EFFECT_SWEEP_TASK_NAME,
    acks_late=True,
    ignore_result=True,
)
def dispatch_survey_response_effect_batch_task() -> dict[str, Any]:
    """Optional broker wakeup; the permanent database poller remains authoritative."""

    return dispatch_survey_response_effect_batch()


def summarize_survey_response_effect_worker() -> dict[str, Any]:
    """Return aggregate, payload-free health for deployment probes/operators."""

    rows = (
        db.session.query(
            SurveyResponseEffect.status,
            func.count(SurveyResponseEffect.id),
        )
        .group_by(SurveyResponseEffect.status)
        .all()
    )
    by_status = {str(status): int(count or 0) for status, count in rows}
    due_tenants = list_due_survey_response_effect_tenant_ids()
    return {
        "contract_version": SURVEY_RESPONSE_EFFECT_WORKER_HEALTH_CONTRACT,
        "total": sum(by_status.values()),
        "due_tenant_count": len(due_tenants),
        "dead": int(by_status.get("dead", 0)),
        "by_status": by_status,
    }


def run_survey_response_effect_worker(
    app: Any,
    *,
    once: bool = False,
    stop_event: Optional[threading.Event] = None,
    batch_limit: Optional[int] = None,
    deadline_monotonic: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    require_current_schema: bool = False,
    require_shared_realtime: bool = False,
) -> dict[str, Any]:
    """Run the standalone poller used by the Render background worker."""

    shutdown = stop_event or threading.Event()
    totals = {
        "cycles": 0,
        "claimed": 0,
        "processed": 0,
        "succeeded": 0,
        "skipped": 0,
        "retry_wait": 0,
        "dead": 0,
        "fenced": 0,
        "cycle_failures": 0,
    }
    with app.app_context():
        # Validate every bound at startup. The first health query also fails
        # loudly if the migration/table is missing.
        assert_survey_response_effect_worker_schema_current(
            required=require_current_schema,
        )
        _configured_batch_size()
        _configured_max_tenants()
        _configured_lease_seconds()
        poll_seconds = _configured_poll_seconds()
        summarize_survey_response_effect_worker()

        while not shutdown.is_set():
            totals["cycles"] += 1
            try:
                dispatch_kwargs: dict[str, Any] = {}
                if batch_limit is not None:
                    dispatch_kwargs["limit"] = batch_limit
                if deadline_monotonic is not None:
                    dispatch_kwargs.update(
                        deadline_monotonic=deadline_monotonic,
                        clock=clock,
                    )
                if require_shared_realtime:
                    dispatch_kwargs["require_shared_realtime"] = True
                report = dispatch_survey_response_effect_batch(**dispatch_kwargs)
                for key in (
                    "claimed",
                    "processed",
                    "succeeded",
                    "skipped",
                    "retry_wait",
                    "dead",
                    "fenced",
                ):
                    totals[key] += int(report.get(key) or 0)
                did_work = bool(int(report.get("claimed") or 0))
                db.session.remove()
            except Exception:
                db.session.rollback()
                db.session.remove()
                totals["cycle_failures"] += 1
                logger.exception(
                    "[SURVEY_RESPONSE_EFFECT_WORKER] poll_cycle_failed"
                )
                did_work = False

            if once:
                break
            if not did_work:
                shutdown.wait(poll_seconds)

    return {
        "contract_version": SURVEY_RESPONSE_EFFECT_WORKER_RUN_CONTRACT,
        **totals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chatboc durable survey-response effect worker"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("CHATBOC_PROCESS_ROLE", "survey-effect-worker")
    os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
    from app import create_app
    from config import Config

    app = create_app(Config)
    if args.health:
        with app.app_context():
            assert_survey_response_effect_worker_schema_current()
            print(
                json.dumps(
                    summarize_survey_response_effect_worker(),
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
        return 0

    shutdown = threading.Event()

    def _stop(*_args: Any) -> None:
        shutdown.set()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signal_name, _stop)
        except (AttributeError, ValueError):
            pass

    run_survey_response_effect_worker(
        app,
        once=args.once,
        stop_event=shutdown,
    )
    return 0


__all__ = [
    "SURVEY_RESPONSE_EFFECT_SWEEP_TASK_NAME",
    "SurveyResponseEffectWorkerConfigurationError",
    "assert_survey_response_effect_worker_schema_current",
    "dispatch_survey_response_effect_batch",
    "dispatch_survey_response_effect_batch_task",
    "run_survey_response_effect_worker",
    "summarize_survey_response_effect_worker",
]


if __name__ == "__main__":
    raise SystemExit(main())
