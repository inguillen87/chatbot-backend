"""Fail-closed internal endpoints invoked by Vercel Cron."""

from __future__ import annotations

import hmac

from flask import Blueprint, current_app, jsonify, request


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


def _maintenance_crons_are_enabled() -> bool:
    return current_app.config.get("VERCEL_MAINTENANCE_CRONS_ENABLED") is True


def _json_no_store(payload: dict, status_code: int):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


def _maintenance_gate():
    if not _has_valid_cron_authorization():
        return _json_no_store(
            {
                "contract_version": "internal.cron.authorization.v1",
                "status": "unauthorized",
            },
            401,
        )

    if not _maintenance_crons_are_enabled():
        return _json_no_store(
            {
                "contract_version": "internal.cron.activation.v1",
                "executed": False,
                "reason_code": "vercel_maintenance_crons_disabled",
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

    payload = run_outbox_reconciliation(current_app._get_current_object())
    response = jsonify(payload)
    response.status_code = 200 if payload.get("ok") is True else 503
    response.headers["Cache-Control"] = "no-store"
    return response


@internal_cron_bp.get("/whatsapp-payload-retention")
def whatsapp_payload_retention():
    gate_response = _maintenance_gate()
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
    gate_response = _maintenance_gate()
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


__all__ = ["internal_cron_bp"]
