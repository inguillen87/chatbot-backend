"""Admin AI endpoints with tenant isolation."""

from __future__ import annotations

from datetime import datetime
from flask import Blueprint, abort, jsonify, request

from models import MunicipioTicket, PymeTicket, TicketComentario
from services.analytics import get_summary
from services.analytics.filters import parse_filters
from services.analytics.rbac import require_access
from services.openai_bridge import generate_analytics_report, generate_ticket_summary

admin_ai_bp = Blueprint("admin_ai_bp", __name__, url_prefix="/admin")


@admin_ai_bp.post("/ai/executive-summary")
def executive_summary():
    payload = request.get_json(silent=True) or {}
    tenant_id = payload.get("tenant_id")
    if tenant_id is None:
        abort(400, description="tenant_id is required")
    try:
        tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be an integer")

    filters = parse_filters(
        {
            "tenant_id": str(tenant_id),
            "scope": payload.get("scope") or "pyme",
            "from": payload.get("from"),
            "to": payload.get("to"),
        }
    )
    require_access(filters.tenant_id, "operador")

    metrics = get_summary(filters)
    report = generate_analytics_report(metrics, tenant_type=filters.scope)

    return jsonify({
        "tenant_id": tenant_id,
        "scope": filters.scope,
        "from": filters.date_from.isoformat() if filters.date_from else None,
        "to": filters.date_to.isoformat() if filters.date_to else None,
        "metrics": metrics,
        "ai": report,
    })


@admin_ai_bp.post("/tickets/<int:ticket_id>/ai-summary")
def ticket_ai_summary(ticket_id: int):
    payload = request.get_json(silent=True) or {}
    scope = (payload.get("scope") or "municipio").lower()
    if scope not in {"municipio", "pyme"}:
        abort(400, description="scope must be municipio|pyme")

    model = MunicipioTicket if scope == "municipio" else PymeTicket
    ticket = model.query.get(ticket_id)
    if not ticket:
        abort(404, description="ticket not found")

    tenant_hint = getattr(ticket, "tenant_id", None)
    if not tenant_hint:
        tenant_hint = getattr(ticket, "municipio_id", None) or getattr(ticket, "user_id", None)
    if not tenant_hint:
        abort(404, description="ticket tenant not resolved")

    require_access(str(tenant_hint), "operador")

    comments = (
        TicketComentario.query.filter(
            TicketComentario.municipio_ticket_id == ticket_id
            if scope == "municipio"
            else TicketComentario.pyme_ticket_id == ticket_id
        )
        .order_by(TicketComentario.fecha.asc())
        .all()
    )

    ticket_payload = {
        "id": ticket.id,
        "scope": scope,
        "estado": getattr(ticket, "estado", None),
        "categoria": getattr(ticket, "categoria", None),
        "asunto": getattr(ticket, "asunto", None),
        "pregunta": getattr(ticket, "pregunta", None),
        "fecha": ticket.fecha.isoformat() if getattr(ticket, "fecha", None) else None,
        "timeline": [
            {
                "fecha": c.fecha.isoformat() if c.fecha else None,
                "comentario": c.comentario,
                "estado_ticket": c.estado_ticket,
                "es_admin": bool(c.es_admin),
            }
            for c in comments
        ],
    }

    summary = generate_ticket_summary(ticket_payload)
    return jsonify({"ticket_id": ticket.id, "scope": scope, "ai": summary})
