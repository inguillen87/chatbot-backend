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

    # Import lazily so migrations and ordinary requests do not import all
    # standalone worker dependencies only to register this blueprint.
    from services.outbox_reconciliation import run_outbox_reconciliation

    payload = run_outbox_reconciliation(current_app._get_current_object())
    response = jsonify(payload)
    response.status_code = 200 if payload.get("ok") is True else 503
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = ["internal_cron_bp"]
