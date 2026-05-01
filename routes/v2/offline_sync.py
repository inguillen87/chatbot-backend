from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, jsonify, request

offline_sync_bp = Blueprint("offline_sync", __name__, url_prefix="/api")


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _stable_suffix(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"_", "-"}).strip("_-")
    return text[:48] or uuid.uuid4().hex[:12]


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


@offline_sync_bp.route("/surveys/sync", methods=["POST"])
def sync_survey_responses():
    payload = request.get_json(silent=True)
    if isinstance(payload, list):
        synced = len(payload)
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("responses") or payload.get("respuestas")
        synced = len(items) if isinstance(items, list) else 1
    else:
        synced = 0

    return _json_response(
        {
            "ok": True,
            "contract_version": "surveys.sync.v1",
            "synced": synced,
            "idempotency_key": request.headers.get("Idempotency-Key") or (payload or {}).get("idempotency_key")
            if isinstance(payload, dict)
            else request.headers.get("Idempotency-Key"),
        }
    )


@offline_sync_bp.route("/tickets/draft/sync", methods=["POST"])
def sync_ticket_draft():
    payload = request.get_json(silent=True) or {}
    idempotency_key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
    ticket_id = payload.get("ticket_id") or payload.get("id") or payload.get("nro_ticket")
    if not ticket_id:
        suffix = _stable_suffix(idempotency_key or uuid.uuid4().hex[:12]).upper()
        ticket_id = f"TCK-{suffix}"

    return _json_response(
        {
            "ok": True,
            "contract_version": "tickets.draft_sync.v1",
            "ticket_id": str(ticket_id),
            "idempotency_key": idempotency_key,
            "status": "draft_synced",
        }
    )
