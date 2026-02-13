"""Admin AI endpoints with tenant isolation."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from flask import Blueprint, abort, jsonify, request

from models import CatalogoItem, MunicipioTicket, PymePedido, PymeTicket, TicketComentario
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


@admin_ai_bp.post("/ai/product-recommendations")
def product_recommendations():
    payload = request.get_json(silent=True) or {}
    tenant_id = payload.get("tenant_id")
    if tenant_id is None:
        abort(400, description="tenant_id is required")
    try:
        tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be an integer")

    require_access(str(tenant_id), "operador")
    limit = payload.get("limit", 5)
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5

    items = (
        CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant_id, CatalogoItem.disponible.is_(True))
        .order_by(CatalogoItem.timestamp.desc())
        .all()
    )
    if not items:
        return jsonify({"tenant_id": tenant_id, "recommendations": [], "reason": "no_catalog_data"})

    keyword_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    orders = (
        PymePedido.query.filter(PymePedido.tenant_id == tenant_id)
        .order_by(PymePedido.fecha.desc())
        .limit(200)
        .all()
    )
    for order in orders:
        detalles = order.detalles
        if not detalles:
            continue
        try:
            parsed = json.loads(detalles) if isinstance(detalles, str) else detalles
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            continue
        for line in parsed:
            if not isinstance(line, dict):
                continue
            nombre = str(line.get("nombre") or line.get("product") or "").strip().lower()
            if nombre:
                keyword_counter[nombre] += 1

    for item in items:
        cat = (item.categoria or "").strip().lower()
        if cat:
            category_counter[cat] += 1

    scored = []
    for item in items:
        nombre = (item.nombre or "").strip().lower()
        categoria = (item.categoria or "").strip().lower()
        score = keyword_counter.get(nombre, 0) * 10 + category_counter.get(categoria, 0)
        if item.es_canje:
            score += 2
        if item.es_donacion:
            score += 1
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    recommendations = []
    for score, item in scored[:limit]:
        recommendations.append(
            {
                "catalogo_item_id": item.id,
                "nombre": item.nombre,
                "categoria": item.categoria,
                "modalidad": item.modalidad,
                "precio_monetario": str(item.precio_monetario) if item.precio_monetario is not None else item.precio,
                "precio_puntos": item.precio_puntos,
                "score": score,
                "reason": "historical-demand" if score > 0 else "catalog-coverage",
            }
        )

    return jsonify(
        {
            "tenant_id": tenant_id,
            "orders_analyzed": len(orders),
            "recommendations": recommendations,
        }
    )
