"""Authoritative DB poller for tenant-scoped domain effects."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from typing import Any, Callable, Optional

from flask import current_app, has_app_context

from celery_utils import celery_app
from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
    log_background_writer_fence,
)
from models import db
from services.domain_effect_gate import (
    DomainEffectOutboxConfigurationError,
    resolve_domain_effect_outbox_canaries,
    resolve_domain_effect_outbox_policy,
)
from services.global_writer_authority import (
    background_global_writer_authority_report,
)
from services.domain_effect_outbox import (
    compose_domain_effect_registries,
    dispatch_domain_effects,
    summarize_domain_effect_outbox,
)
from services.order_domain_effects import ORDER_DOMAIN_EFFECT_REGISTRY
from services.ticket_domain_effects import TICKET_DOMAIN_EFFECT_REGISTRY


logger = logging.getLogger(__name__)

DOMAIN_EFFECT_TASK_NAME = "chatboc.domain_effect_outbox.dispatch"
DOMAIN_EFFECT_WORKER_REGISTRY = compose_domain_effect_registries(
    TICKET_DOMAIN_EFFECT_REGISTRY,
    ORDER_DOMAIN_EFFECT_REGISTRY,
)
_ROUND_ROBIN_EXTENSION_KEY = "chatboc.domain_effect_worker.round_robin"
_ROUND_ROBIN_LOCK = threading.Lock()


def _rotate_tenant_ids(tenant_ids: tuple[int, ...]) -> tuple[int, ...]:
    """Rotate canaries once per process cycle without cross-thread races."""

    if len(tenant_ids) < 2:
        return tenant_ids
    with _ROUND_ROBIN_LOCK:
        cursors = current_app.extensions.setdefault(_ROUND_ROBIN_EXTENSION_KEY, {})
        start = int(cursors.get(tenant_ids, 0)) % len(tenant_ids)
        cursors[tenant_ids] = (start + 1) % len(tenant_ids)
    return tenant_ids[start:] + tenant_ids[:start]


def _configured_batch_size() -> int:
    try:
        value = int(current_app.config.get("DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE", 20))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_worker_batch_invalid"
        ) from exc
    if value < 1 or value > 100:
        raise DomainEffectOutboxConfigurationError("domain_effect_worker_batch_invalid")
    return value


def _configured_lease_seconds() -> int:
    try:
        value = int(current_app.config.get("DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS", 180))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_worker_lease_invalid"
        ) from exc
    if value < 30 or value > 3600:
        raise DomainEffectOutboxConfigurationError("domain_effect_worker_lease_invalid")
    return value


def dispatch_domain_effect_batch(
    *,
    tenant_id: Optional[int] = None,
    limit: Optional[int] = None,
    deadline_monotonic: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Dispatch only tenants included in the explicit canary allowlist."""

    if cutover_writer_fence_enabled(
        current_app.config if has_app_context() else None
    ):
        return {
            "contract_version": "domain.effect_worker_batch.v1",
            "status": "fenced",
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "unknown": 0,
            "retry_wait": 0,
            "dead": 0,
            "recovered_unknown": 0,
            "recovered_retry_wait": 0,
            "recovered_dead": 0,
            "tenants": [],
        }
    if not has_app_context():
        raise DomainEffectOutboxConfigurationError("domain_effect_app_context_required")
    authority_report = background_global_writer_authority_report(
        "domain_effect_worker",
        current_app.config,
    )
    if authority_report is not None:
        return {
            **authority_report,
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "unknown": 0,
            "retry_wait": 0,
            "dead": 0,
            "recovered_unknown": 0,
            "recovered_retry_wait": 0,
            "recovered_dead": 0,
            "tenants": [],
        }
    canaries = resolve_domain_effect_outbox_canaries(current_app.config)
    if not canaries:
        if tenant_id is not None:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_worker_requires_queue_mode"
            )
        return {
            "contract_version": "domain.effect_worker_batch.v1",
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "unknown": 0,
            "retry_wait": 0,
            "dead": 0,
            "recovered_unknown": 0,
            "recovered_retry_wait": 0,
            "recovered_dead": 0,
            "tenants": [],
        }

    if tenant_id is not None:
        try:
            requested_tenant = int(tenant_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_worker_tenant_invalid"
            ) from exc
        if requested_tenant not in canaries:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_worker_tenant_not_canary"
            )
        tenant_ids = (requested_tenant,)
    else:
        tenant_ids = _rotate_tenant_ids(tuple(sorted(canaries)))

    configured_limit = _configured_batch_size() if limit is None else int(limit)
    bounded_limit = max(1, min(configured_limit, 100))
    remaining = bounded_limit
    totals = {
        "processed": 0,
        "succeeded": 0,
        "skipped": 0,
        "unknown": 0,
        "retry_wait": 0,
        "dead": 0,
        "recovered_unknown": 0,
        "recovered_retry_wait": 0,
        "recovered_dead": 0,
    }
    per_tenant: list[dict[str, Any]] = []
    for tenant_index, current_tenant_id in enumerate(tenant_ids):
        if remaining <= 0:
            break
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            break
        tenants_remaining = len(tenant_ids) - tenant_index
        # Reserve a deterministic fair share for every remaining canary.  A
        # tenant with a permanently full backlog must not consume the complete
        # process-wide batch before the other configured tenants are polled.
        tenant_limit = max(1, (remaining + tenants_remaining - 1) // tenants_remaining)
        policy = resolve_domain_effect_outbox_policy(
            current_app.config,
            tenant_id=current_tenant_id,
        )
        if not policy.enabled or not policy.secret:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_worker_policy_mismatch"
            )
        dispatch_kwargs: dict[str, Any] = {}
        if deadline_monotonic is not None:
            dispatch_kwargs["should_continue"] = (
                lambda: clock() < deadline_monotonic
            )
        summary = dispatch_domain_effects(
            registry=DOMAIN_EFFECT_WORKER_REGISTRY,
            intent_secret=policy.secret,
            tenant_id=current_tenant_id,
            limit=tenant_limit,
            lease_seconds=_configured_lease_seconds(),
            **dispatch_kwargs,
        )
        report = summary.to_dict()
        report["tenant_id"] = current_tenant_id
        per_tenant.append(report)
        for key in totals:
            totals[key] += int(getattr(summary, key))
        remaining -= int(summary.processed) + sum(
            int(getattr(summary, key))
            for key in (
                "recovered_unknown",
                "recovered_retry_wait",
                "recovered_dead",
            )
        )

    return {
        "contract_version": "domain.effect_worker_batch.v1",
        **totals,
        "tenants": per_tenant,
    }


def enqueue_domain_effect_dispatch(*, tenant_id: int) -> bool:
    """Best-effort broker wakeup; the committed DB outbox stays authoritative."""

    if (
        not has_app_context()
        or cutover_writer_fence_enabled(current_app.config)
        or current_app.testing
        or not bool(
            current_app.config.get(
                "DOMAIN_EFFECT_OUTBOX_CELERY_WAKEUP_ENABLED",
                False,
            )
        )
    ):
        return False
    canaries = resolve_domain_effect_outbox_canaries(current_app.config)
    if int(tenant_id) not in canaries:
        return False
    dispatch_domain_effects_task.apply_async(
        args=[int(tenant_id)],
        retry=False,
        ignore_result=True,
    )
    return True


@celery_app.task(name=DOMAIN_EFFECT_TASK_NAME, acks_late=True, ignore_result=True)
def dispatch_domain_effects_task(tenant_id: Optional[int] = None) -> dict[str, Any]:
    return dispatch_domain_effect_batch(tenant_id=tenant_id)


def run_domain_effect_worker(
    app: Any,
    *,
    once: bool = False,
    stop_event: Optional[threading.Event] = None,
    batch_limit: Optional[int] = None,
    deadline_monotonic: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the database poller used by a dedicated background process."""

    shutdown = stop_event or threading.Event()
    totals = {
        "cycles": 0,
        "processed": 0,
        "succeeded": 0,
        "skipped": 0,
        "retry_wait": 0,
        "unknown": 0,
        "dead": 0,
        "recovered_unknown": 0,
        "recovered_retry_wait": 0,
        "recovered_dead": 0,
        "cycle_failures": 0,
    }
    if cutover_writer_fence_enabled(app.config):
        report = {
            **background_writer_fence_report("domain_effect_worker"),
            **totals,
        }
        log_background_writer_fence(logger, report)
        if not once and not shutdown.is_set():
            shutdown.wait()
        return report
    with app.app_context():
        authority_report = background_global_writer_authority_report(
            "domain_effect_worker",
            current_app.config,
        )
        if authority_report is not None:
            return {**authority_report, **totals}
        resolve_domain_effect_outbox_canaries(current_app.config)
        _configured_batch_size()
        _configured_lease_seconds()
        try:
            poll_seconds = float(
                current_app.config.get("DOMAIN_EFFECT_OUTBOX_WORKER_POLL_SECONDS", 0.5)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_worker_poll_invalid"
            ) from exc
        if poll_seconds < 0.05 or poll_seconds > 60.0:
            raise DomainEffectOutboxConfigurationError("domain_effect_worker_poll_invalid")

        while not shutdown.is_set():
            authority_report = background_global_writer_authority_report(
                "domain_effect_worker",
                current_app.config,
            )
            if authority_report is not None:
                return {**authority_report, **totals}
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
                report = dispatch_domain_effect_batch(**dispatch_kwargs)
                for key in (
                    "processed",
                    "succeeded",
                    "skipped",
                    "retry_wait",
                    "unknown",
                    "dead",
                    "recovered_unknown",
                    "recovered_retry_wait",
                    "recovered_dead",
                ):
                    totals[key] += int(report.get(key) or 0)
                did_work = bool(
                    int(report["processed"])
                    + int(report.get("recovered_unknown") or 0)
                    + int(report.get("recovered_retry_wait") or 0)
                    + int(report.get("recovered_dead") or 0)
                )
                db.session.remove()
            except Exception:
                db.session.rollback()
                db.session.remove()
                totals["cycle_failures"] += 1
                logger.exception("[DOMAIN_EFFECT_WORKER] poll_cycle_failed")
                did_work = False
            if once:
                break
            if not did_work:
                shutdown.wait(poll_seconds)
    return {"contract_version": "domain.effect_worker_run.v1", **totals}


def main() -> int:
    parser = argparse.ArgumentParser(description="Chatboc durable domain-effect worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--tenant-id", type=int)
    args = parser.parse_args()

    os.environ.setdefault("CHATBOC_PROCESS_ROLE", "domain-effect-worker")
    os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
    shutdown = threading.Event()

    def _stop(*_args: Any) -> None:
        shutdown.set()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signal_name, _stop)
        except (AttributeError, ValueError):
            pass
    if cutover_writer_fence_enabled():
        report = {
            **background_writer_fence_report("domain_effect_worker"),
            "cycles": 0,
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "retry_wait": 0,
            "unknown": 0,
            "dead": 0,
            "recovered_unknown": 0,
            "recovered_retry_wait": 0,
            "recovered_dead": 0,
            "cycle_failures": 0,
        }
        log_background_writer_fence(logger, report)
        if args.once or args.health or args.tenant_id is not None:
            print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        elif not shutdown.is_set():
            shutdown.wait()
        return 0

    from app import create_app
    from config import Config

    app = create_app(Config)
    if args.health:
        with app.app_context():
            report = summarize_domain_effect_outbox(tenant_id=args.tenant_id)
            print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 0
    if args.tenant_id is not None:
        with app.app_context():
            dispatch_domain_effect_batch(tenant_id=args.tenant_id)
        return 0
    run_domain_effect_worker(app, once=args.once, stop_event=shutdown)
    return 0


__all__ = [
    "DOMAIN_EFFECT_WORKER_REGISTRY",
    "dispatch_domain_effect_batch",
    "dispatch_domain_effects_task",
    "enqueue_domain_effect_dispatch",
    "run_domain_effect_worker",
]


if __name__ == "__main__":
    raise SystemExit(main())
