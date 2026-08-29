"""Fail-closed internal endpoints invoked by Vercel Cron."""

from __future__ import annotations

import hmac

from flask import Blueprint, current_app, jsonify, request

from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)
from services.global_writer_authority import (
    background_global_writer_authority_report,
)


internal_cron_bp = Blueprint(
    "internal_cron",
    __name__,
    url_prefix="/api/internal/cron",
)


def _has_valid_cron_authorization() -> bool:
    configured_secret = current_app.config.get("CRON_SECRET")
    if (
        not isinstance(configured_secret, str)
        or len(configured_secret.encode("utf-8")) < 32
    ):
        return False

    provided = request.headers.get("Authorization")
    if not isinstance(provided, str) or not provided:
        return False

    return hmac.compare_digest(
        provided.encode("utf-8"),
        f"Bearer {configured_secret}".encode("utf-8"),
    )


def _outbox_cron_is_enabled() -> bool:
    return current_app.config.get("VERCEL_OUTBOX_CRON_ENABLED") is True


def _retention_cron_is_enabled(flag_name: str) -> bool:
    return current_app.config.get(flag_name) is True


def _weekly_analytics_cron_is_enabled() -> bool:
    return current_app.config.get("VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED") is True


def _json_no_store(payload: dict, status_code: int):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


@internal_cron_bp.before_request
def _enforce_cutover_writer_fence():
    """Fence mutating GET crons before auth, service imports or I/O."""

    if cutover_writer_fence_enabled(current_app.config):
        report = background_writer_fence_report("internal_cron")
    else:
        report = background_global_writer_authority_report(
            "internal_cron",
            current_app.config,
        )
        if report is None:
            return None
    response = _json_no_store(report, 503)
    response.headers["Retry-After"] = "60"
    return response


def _retention_gate(*, flag_name: str, disabled_reason_code: str):
    if not _has_valid_cron_authorization():
        return _json_no_store(
            {
                "contract_version": "internal.cron.authorization.v1",
                "status": "unauthorized",
            },
            401,
        )

    if not _retention_cron_is_enabled(flag_name):
        return _json_no_store(
            {
                "contract_version": "internal.cron.activation.v1",
                "executed": False,
                "reason_code": disabled_reason_code,
                "status": "disabled",
            },
            503,
        )

    return None


def _weekly_analytics_gate():
    if not _has_valid_cron_authorization():
        return _json_no_store(
            {
                "contract_version": "internal.cron.authorization.v1",
                "status": "unauthorized",
            },
            401,
        )

    if not _weekly_analytics_cron_is_enabled():
        return _json_no_store(
            {
                "contract_version": "internal.cron.activation.v1",
                "executed": False,
                "reason_code": "vercel_weekly_analytics_cron_disabled",
                "status": "disabled",
            },
            503,
        )

    return None


def _report_count(report: dict, key: str, *, maximum: int | None = None) -> int:
    value = report.get(key, 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or (maximum is not None and value > maximum)
    ):
        raise RuntimeError("maintenance_cron_report_invalid")
    return value


def _validate_outbox_reconciliation_report(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError("outbox_reconciliation_report_invalid")
    status = payload.get("status")
    successful_statuses = {
        "completed",
        "contended",
        "drain_limit_reached",
        "time_budget_reached",
    }
    failed_statuses = {
        "configuration_unavailable",
        "degraded",
        "reconciliation_unavailable",
    }
    if (
        payload.get("contract_version") != "outbox.reconciliation.v2"
        or not isinstance(payload.get("ok"), bool)
        or not isinstance(payload.get("has_more"), bool)
        or payload.get("idempotency") != "database_outbox_leases_and_fencing"
        or payload.get("exclusive_lease")
        != "postgresql_transaction_advisory_lock"
        or not isinstance(payload.get("limits"), dict)
        or status not in successful_statuses | failed_statuses
        or payload["ok"] is not (status in successful_statuses)
    ):
        raise RuntimeError("outbox_reconciliation_report_invalid")
    _report_count(payload, "elapsed_ms")

    component_count = _report_count(payload, "component_count", maximum=3)
    failed_count = _report_count(
        payload,
        "failed_component_count",
        maximum=component_count,
    )
    attention_count = _report_count(
        payload,
        "attention_component_count",
        maximum=component_count,
    )
    cycles_run = _report_count(payload, "cycles_run", maximum=20)
    components = payload.get("components")
    expected_component_names = {
        "whatsapp",
        "domain_effects",
        "survey_effects",
    }
    if (
        not isinstance(components, dict)
        or len(components) != component_count
        or failed_count + attention_count > component_count
    ):
        raise RuntimeError("outbox_reconciliation_report_invalid")

    if status == "configuration_unavailable":
        if (
            components
            or component_count != 0
            or failed_count != 0
            or attention_count != 0
            or cycles_run != 0
            or payload["has_more"] is not False
        ):
            raise RuntimeError("outbox_reconciliation_report_invalid")
        return payload

    if component_count != 3 or set(components) != expected_component_names:
        raise RuntimeError("outbox_reconciliation_report_invalid")

    allowed_component_statuses = {
        "not_run",
        "standby",
        "idle",
        "progress",
        "retry_wait",
        "fenced",
        "failed",
        "attention_required",
        "deferred_time_budget",
    }
    failed_status_count = 0
    attention_status_count = 0
    for component in components.values():
        if (
            not isinstance(component, dict)
            or component.get("status") not in allowed_component_statuses
        ):
            raise RuntimeError("outbox_reconciliation_report_invalid")
        _report_count(component, "attempts", maximum=20)
        _report_count(component, "cycle_failures", maximum=20)
        if component["status"] in {"failed", "retry_wait", "fenced"}:
            failed_status_count += 1
        elif component["status"] == "attention_required":
            attention_status_count += 1
    if (
        failed_status_count != failed_count
        or attention_status_count != attention_count
    ):
        raise RuntimeError("outbox_reconciliation_report_invalid")

    if status == "completed":
        valid_status_shape = (
            failed_count == 0
            and attention_count == 0
            and cycles_run > 0
            and payload["has_more"] is False
        )
    elif status == "contended":
        valid_status_shape = (
            failed_count == 0
            and attention_count == 0
            and cycles_run == 0
            and payload["has_more"] is True
        )
    elif status == "drain_limit_reached":
        valid_status_shape = (
            failed_count == 0
            and attention_count == 0
            and cycles_run > 0
            and payload["has_more"] is True
        )
    elif status == "time_budget_reached":
        valid_status_shape = (
            failed_count == 0
            and attention_count == 0
            and payload["has_more"] is True
        )
    elif status == "degraded":
        valid_status_shape = (
            failed_count + attention_count > 0
            and payload["has_more"] is True
        )
    else:
        valid_status_shape = payload["has_more"] is True
    if not valid_status_shape:
        raise RuntimeError("outbox_reconciliation_report_invalid")
    return payload


@internal_cron_bp.get("/outbox-reconciliation")
def outbox_reconciliation():
    if not _has_valid_cron_authorization():
        response = jsonify(
            {
                "contract_version": "internal.cron.authorization.v1",
                "status": "unauthorized",
            }
        )
        response.status_code = 401
        response.headers["Cache-Control"] = "no-store"
        return response

    if not _outbox_cron_is_enabled():
        response = jsonify(
            {
                "contract_version": "internal.cron.activation.v1",
                "executed": False,
                "reason_code": "vercel_outbox_cron_disabled",
                "status": "disabled",
            }
        )
        response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response

    # Import lazily so migrations and ordinary requests do not import all
    # standalone worker dependencies only to register this blueprint.
    from services.outbox_reconciliation import run_outbox_reconciliation

    try:
        payload = _validate_outbox_reconciliation_report(
            run_outbox_reconciliation(current_app._get_current_object())
        )
    except Exception as exc:
        current_app.logger.error(
            "Outbox reconciliation failed; error_type=%s",
            type(exc).__name__,
        )
        return _json_no_store(
            {
                "contract_version": "internal.outbox_reconciliation.v1",
                "executed": True,
                "ok": False,
                "status": "failed",
            },
            503,
        )
    response = jsonify(payload)
    response.status_code = 200 if payload.get("ok") is True else 503
    response.headers["Cache-Control"] = "no-store"
    return response


@internal_cron_bp.get("/whatsapp-payload-retention")
def whatsapp_payload_retention():
    gate_response = _retention_gate(
        flag_name="VERCEL_WHATSAPP_PAYLOAD_RETENTION_CRON_ENABLED",
        disabled_reason_code=(
            "vercel_whatsapp_payload_retention_cron_disabled"
        ),
    )
    if gate_response is not None:
        return gate_response

    # This module reaches the database and provider integrations at import
    # time. Keep it behind both the bearer and independent cutover gate.
    try:
        from services.whatsapp_inbound_worker import (
            run_whatsapp_inbound_payload_scrub,
        )

        report = run_whatsapp_inbound_payload_scrub()
        if report.get("contract_version") != "whatsapp.inbound_payload_scrub_batch.v1":
            raise RuntimeError("whatsapp_retention_report_invalid")
        status = report.get("status")
        if status not in {
            "completed",
            "disabled",
            "legal_hold",
            "no_retention_tenants",
        }:
            raise RuntimeError("whatsapp_retention_report_invalid")
        payload = {
            "contract_version": "internal.maintenance_cron.v1",
            "executed": status == "completed",
            "job": "whatsapp_payload_retention",
            "status": status,
            "tenant_count": _report_count(report, "tenant_count"),
            "selected": _report_count(report, "selected", maximum=500),
            "scrubbed": _report_count(report, "scrubbed", maximum=500),
            "completed_scrubbed": _report_count(
                report,
                "completed_scrubbed",
                maximum=500,
            ),
            "dead_scrubbed": _report_count(
                report,
                "dead_scrubbed",
                maximum=500,
            ),
        }
        if status == "completed":
            payload["batch_limit"] = _report_count(
                report,
                "batch_limit",
                maximum=500,
            )
            payload["batch_remaining"] = _report_count(
                report,
                "batch_remaining",
                maximum=payload["batch_limit"],
            )
            if payload["batch_limit"] < 1:
                raise RuntimeError("whatsapp_retention_report_invalid")
            if payload["selected"] + payload["batch_remaining"] != payload["batch_limit"]:
                raise RuntimeError("whatsapp_retention_report_invalid")
        elif any(
            payload[key] != 0
            for key in {
                "tenant_count",
                "selected",
                "scrubbed",
                "completed_scrubbed",
                "dead_scrubbed",
            }
        ):
            raise RuntimeError("whatsapp_retention_report_invalid")
        if (
            payload["scrubbed"] > payload["selected"]
            or payload["completed_scrubbed"] + payload["dead_scrubbed"]
            != payload["scrubbed"]
        ):
            raise RuntimeError("whatsapp_retention_report_invalid")
    except Exception as exc:
        current_app.logger.error(
            "WhatsApp payload retention failed; error_type=%s",
            type(exc).__name__,
        )
        return _json_no_store(
            {
                "contract_version": "internal.maintenance_cron.v1",
                "error_type": type(exc).__name__,
                "executed": True,
                "job": "whatsapp_payload_retention",
                "status": "failed",
            },
            503,
        )
    return _json_no_store(payload, 200)


@internal_cron_bp.get("/survey-privacy-retention")
def survey_privacy_retention():
    gate_response = _retention_gate(
        flag_name="VERCEL_SURVEY_PRIVACY_RETENTION_CRON_ENABLED",
        disabled_reason_code="vercel_survey_privacy_retention_cron_disabled",
    )
    if gate_response is not None:
        return gate_response

    # Import only after both gates. The service imports the ORM/models and the
    # call below is the same bounded operation used by the Render cron.
    try:
        from services.survey_privacy import run_retention_purge_batches

        report = run_retention_purge_batches(
            batch_size=200,
            max_batches=20,
            dry_run=False,
        )
        if (
            report.get("contract_version")
            != "surveys.privacy_retention_purge.v1"
            or report.get("dry_run") is not False
            or not isinstance(report.get("exhausted_batch_budget"), bool)
        ):
            raise RuntimeError("survey_privacy_report_invalid")
        exhausted = report["exhausted_batch_budget"]
        payload = {
            "contract_version": "internal.maintenance_cron.v1",
            "executed": True,
            "job": "survey_privacy_retention",
            "status": "batch_budget_exhausted" if exhausted else "completed",
            "batches": _report_count(report, "batches", maximum=20),
            "eligible": _report_count(report, "eligible", maximum=4000),
            "deleted": _report_count(report, "deleted", maximum=4000),
            "exhausted_batch_budget": exhausted,
        }
        if payload["batches"] < 1 or payload["deleted"] != payload["eligible"]:
            raise RuntimeError("survey_privacy_report_invalid")
    except Exception as exc:
        current_app.logger.error(
            "Survey privacy retention failed; error_type=%s",
            type(exc).__name__,
        )
        return _json_no_store(
            {
                "contract_version": "internal.maintenance_cron.v1",
                "error_type": type(exc).__name__,
                "executed": True,
                "job": "survey_privacy_retention",
                "status": "failed",
            },
            503,
        )
    return _json_no_store(payload, 503 if exhausted else 200)


@internal_cron_bp.get("/weekly-analytics-report")
def weekly_analytics_report():
    gate_response = _weekly_analytics_gate()
    if gate_response is not None:
        return gate_response

    try:
        from services.weekly_analytics_reports import run_weekly_analytics_batch

        report = run_weekly_analytics_batch(current_app._get_current_object())
        if (
            report.get("contract_version") != "weekly.analytics_report_run.v1"
            or not isinstance(report.get("ok"), bool)
            or not isinstance(report.get("has_more"), bool)
            or report.get("status")
            not in {
                "completed",
                "contended",
                "batch_limit_reached",
                "degraded",
                "configuration_unavailable",
                "database_unavailable",
            }
        ):
            raise RuntimeError("weekly_analytics_report_invalid")
        payload = {
            "contract_version": "internal.weekly_analytics_cron.v1",
            "executed": _report_count(
                report,
                "provider_attempts",
                maximum=10,
            )
            > 0,
            "job": "weekly_analytics_report",
            "ok": report["ok"],
            "status": report["status"],
            "batch_limit": _report_count(report, "batch_limit", maximum=10),
            "selected": _report_count(report, "selected", maximum=10),
            "provider_attempts": _report_count(
                report,
                "provider_attempts",
                maximum=10,
            ),
            "reports_generated": _report_count(
                report,
                "reports_generated",
                maximum=10,
            ),
            "contended": _report_count(report, "contended", maximum=10),
            "failed_before_provider": _report_count(
                report,
                "failed_before_provider",
                maximum=10,
            ),
            "provider_uncertain": _report_count(
                report,
                "provider_uncertain",
                maximum=10,
            ),
            "reservation_failures": _report_count(
                report,
                "reservation_failures",
                maximum=10,
            ),
            "unresolved_reservations": _report_count(
                report,
                "unresolved_reservations",
            ),
            "has_more": report["has_more"],
        }
        if (
            payload["ok"]
            is not (payload["status"] in {"completed", "batch_limit_reached"})
            or payload["provider_attempts"] > payload["selected"]
            or payload["reports_generated"] > payload["provider_attempts"]
            or payload["provider_uncertain"] > payload["provider_attempts"]
            or payload["reports_generated"] + payload["provider_uncertain"]
            > payload["provider_attempts"]
            or (
                payload["status"] != "configuration_unavailable"
                and payload["batch_limit"] < 1
            )
        ):
            raise RuntimeError("weekly_analytics_report_invalid")
    except Exception as exc:
        current_app.logger.error(
            "Weekly analytics cron failed; error_type=%s",
            type(exc).__name__,
        )
        return _json_no_store(
            {
                "contract_version": "internal.weekly_analytics_cron.v1",
                "executed": False,
                "job": "weekly_analytics_report",
                "ok": False,
                "status": "failed",
            },
            503,
        )
    return _json_no_store(payload, 200 if report["ok"] else 503)


__all__ = ["internal_cron_bp"]
