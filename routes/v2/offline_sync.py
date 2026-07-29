from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, jsonify, request

offline_sync_bp = Blueprint("offline_sync", __name__, url_prefix="/api")

OFFLINE_SYNC_RETIRED_CONTRACT_VERSION = "offline.sync.retired.v1"


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _retired_sync_response(*, canonical_endpoint: str):
    return _json_response(
        {
            "contract_version": OFFLINE_SYNC_RETIRED_CONTRACT_VERSION,
            "ok": False,
            "persisted": False,
            "reason_code": "non_durable_sync_endpoint",
            "retryable": False,
            "action_hint": "use_canonical_endpoint",
            "canonical_endpoint": canonical_endpoint,
        },
        410,
    )


@offline_sync_bp.route("/surveys/sync", methods=["POST"])
def sync_survey_responses():
    return _retired_sync_response(
        canonical_endpoint="/api/v2/public/surveys/{public_token}/respond",
    )


@offline_sync_bp.route("/tickets/draft/sync", methods=["POST"])
def sync_ticket_draft():
    return _retired_sync_response(
        canonical_endpoint="/api/pwa/app/tickets",
    )
