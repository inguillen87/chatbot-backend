"""Bounded reconciliation for durable outboxes on scheduled runtimes.

The existing standalone workers remain the authoritative implementation.  A
Vercel Cron invocation only asks each worker for one bounded cycle, preserving
the legacy/queue rollout gates already enforced by those workers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from services.domain_effect_worker import run_domain_effect_worker
from services.survey_response_effect_worker import (
    run_survey_response_effect_worker,
)
from services.whatsapp_inbound_worker import run_whatsapp_durable_worker


logger = logging.getLogger(__name__)

OUTBOX_RECONCILIATION_CONTRACT_VERSION = "outbox.reconciliation.v1"

_REPORT_FIELDS = (
    "contract_version",
    "status",
    "mode",
    "cycles",
    "claimed",
    "processed",
    "inbound_completed",
    "outbound_accepted",
    "unknown",
    "retry_wait",
    "dead",
    "cycle_failures",
)


def _payload_safe_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the cron response useful without returning tenant/effect payloads."""

    return {
        field: report[field]
        for field in _REPORT_FIELDS
        if field in report
    }


def _reconciliation_runners(app: Any) -> tuple[tuple[str, Callable[[], Any]], ...]:
    return (
        (
            "whatsapp",
            lambda: run_whatsapp_durable_worker(
                app,
                once=True,
                standby_when_legacy=True,
            ),
        ),
        (
            "domain_effects",
            lambda: run_domain_effect_worker(app, once=True),
        ),
        (
            "survey_effects",
            lambda: run_survey_response_effect_worker(app, once=True),
        ),
    )


def run_outbox_reconciliation(app: Any) -> dict[str, Any]:
    """Run one isolated, bounded cycle for every durable outbox worker."""

    components: dict[str, dict[str, Any]] = {}
    failed = 0

    for component, runner in _reconciliation_runners(app):
        try:
            raw_report = runner()
            report = (
                raw_report
                if isinstance(raw_report, Mapping)
                else {"contract_version": "unknown"}
            )
            cycle_failures = int(report.get("cycle_failures") or 0)
            component_failed = cycle_failures > 0
            components[component] = {
                "status": "failed" if component_failed else "completed",
                "report": _payload_safe_summary(report),
            }
            if component_failed:
                failed += 1
        except Exception as exc:
            failed += 1
            components[component] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            # Exception messages can contain database URLs, provider payloads
            # or phone numbers.  Log only the type and continue with peers.
            logger.error(
                "[OUTBOX_RECONCILIATION] component_failed component=%s error_type=%s",
                component,
                type(exc).__name__,
            )

    return {
        "contract_version": OUTBOX_RECONCILIATION_CONTRACT_VERSION,
        "ok": failed == 0,
        "status": "completed" if failed == 0 else "degraded",
        "component_count": len(components),
        "failed_component_count": failed,
        "components": components,
    }


__all__ = [
    "OUTBOX_RECONCILIATION_CONTRACT_VERSION",
    "run_outbox_reconciliation",
]
