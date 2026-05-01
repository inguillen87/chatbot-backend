from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, g, jsonify, request

from extensions import db
from models import TenantTicket
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.v2.ticket_event_service import list_ticket_events
from services.v2.ticket_service import add_comment, create_ticket, list_tickets, patch_ticket, serialize_comment, serialize_ticket

v2_tickets_bp = Blueprint("v2_tickets", __name__, url_prefix="/api/v2")


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str = "request_error", action_hint: str = "check_request"):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
            "message": message,
        },
        status_code,
    )


def _viewer():
    return getattr(g, "viewer", None)


def _resolve_tenant_or_error():
    explicit_slug = (
        request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()
    if not explicit_slug:
        return None, _error_response("X-Tenant-Slug es obligatorio en tickets v2", 400, "missing_tenant", "send_tenant_slug")
    try:
        return resolve_tenant_v2(required=True, explicit_slug=explicit_slug), None
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "check_tenant_slug")


@v2_tickets_bp.route("/tickets", methods=["GET"])
def list_tickets_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    filters = {
        "status": request.args.get("status"),
        "priority": request.args.get("priority"),
        "assignee_id": request.args.get("assignee_id"),
        "category": request.args.get("category"),
        "channel": request.args.get("channel"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "q": request.args.get("q"),
        "page": request.args.get("page"),
        "per_page": request.args.get("per_page"),
    }

    items, pagination, summary = list_tickets(tenant=tenant, viewer=_viewer(), filters=filters)
    db.session.commit()
    return _json_response({"contract_version": "tickets.v2.list", "items": items, "pagination": pagination, "summary": summary})


@v2_tickets_bp.route("/tickets", methods=["POST"])
def create_ticket_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    try:
        ticket = create_ticket(tenant=tenant, actor_user=_viewer(), payload=payload)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    return jsonify(serialize_ticket(ticket, viewer=_viewer())), 201


@v2_tickets_bp.route("/tickets/<int:ticket_id>", methods=["PATCH"])
def patch_ticket_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    ticket = TenantTicket.query.get(ticket_id)
    if not ticket or ticket.tenant_id != tenant.id:
        return jsonify({"error": "ticket no encontrado"}), 404

    payload = request.get_json(silent=True) or {}
    try:
        updated = patch_ticket(tenant=tenant, actor_user=_viewer(), ticket=ticket, payload=payload)
        db.session.commit()
    except LookupError:
        db.session.rollback()
        return jsonify({"error": "ticket no encontrado"}), 404

    return jsonify(serialize_ticket(updated, viewer=_viewer()))


@v2_tickets_bp.route("/tickets/<int:ticket_id>/comments", methods=["POST"])
def add_ticket_comment_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    ticket = TenantTicket.query.get(ticket_id)
    if not ticket or ticket.tenant_id != tenant.id:
        return jsonify({"error": "ticket no encontrado"}), 404

    payload = request.get_json(silent=True) or {}
    try:
        comment = add_comment(
            tenant=tenant,
            actor_user=_viewer(),
            ticket=ticket,
            body=payload.get("body") or "",
            visibility=payload.get("visibility") or "public",
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    return jsonify(serialize_comment(comment)), 201


@v2_tickets_bp.route("/tickets/<int:ticket_id>/events", methods=["GET"])
def list_ticket_events_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    ticket = TenantTicket.query.get(ticket_id)
    if not ticket or ticket.tenant_id != tenant.id:
        return jsonify({"error": "ticket no encontrado"}), 404

    events = list_ticket_events(tenant_id=tenant.id, ticket_id=ticket.id)
    items = [
        {
            "id": event.id,
            "event_type": event.event_type,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "details": event.details or {},
            "actor_user_id": event.actor_user_id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
        for event in events
    ]
    return jsonify({"items": items})
