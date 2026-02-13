"""Admin AI endpoints with tenant isolation."""

from __future__ import annotations

import json
import io
from collections import Counter
from datetime import datetime
from flask import Blueprint, abort, jsonify, request
import pdfplumber

from models import CatalogoItem, MunicipioTicket, PymePedido, PymeTicket, TicketComentario
from services.analytics import get_summary
from services.analytics.filters import parse_filters
from services.analytics.rbac import require_access
from services.openai_bridge import generate_analytics_report, generate_ticket_summary
from services.vision_fallback_service import analyze_image_text, analyze_text_structured

admin_ai_bp = Blueprint("admin_ai_bp", __name__, url_prefix="/admin")

_ALLOWED_ORDER_DRAFT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
_MAX_ORDER_DRAFT_BYTES = 5 * 1024 * 1024


def _parse_tenant_id(value):
    if value is None:
        abort(400, description="tenant_id is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be an integer")


@admin_ai_bp.post("/ai/executive-summary")
def executive_summary():
    payload = request.get_json(silent=True) or {}
    tenant_id = _parse_tenant_id(payload.get("tenant_id"))

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
    totals = metrics.get("totals") if isinstance(metrics, dict) else {}
    strict_no_data = bool(payload.get("strict_no_data_message"))
    if strict_no_data and isinstance(totals, dict) and all((totals.get(k) in {0, None, 0.0}) for k in ("tickets", "pedidos")):
        report = {
            "summary": "No hay datos suficientes para generar un resumen ejecutivo en el período seleccionado.",
            "opportunities": [],
            "threats": [],
            "tone": "Data-Insufficient",
        }
    else:
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
    tenant_id = _parse_tenant_id(payload.get("tenant_id"))

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


@admin_ai_bp.post("/ai/order-draft-from-document")
def order_draft_from_document():
    """Build a preliminary order draft from uploaded PDF/image and tenant catalog."""
    tenant_id = _parse_tenant_id(request.form.get("tenant_id") or request.args.get("tenant_id"))

    require_access(str(tenant_id), "operador")

    uploaded = request.files.get("file")
    if not uploaded:
        abort(400, description="file is required")

    filename = (uploaded.filename or "").lower()
    if not any(filename.endswith(ext) for ext in _ALLOWED_ORDER_DRAFT_EXTENSIONS):
        abort(400, description="unsupported file type")

    content = uploaded.read() or b""
    if not content:
        abort(400, description="file is empty")
    if len(content) > _MAX_ORDER_DRAFT_BYTES:
        abort(413, description="file too large")
    extracted_text = ""

    if filename.endswith(".pdf"):
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                chunks = []
                for page in pdf.pages[:5]:
                    chunks.append(page.extract_text() or "")
                extracted_text = "\n".join(chunks).strip()
        except Exception:  # noqa: BLE001
            extracted_text = ""
    else:
        try:
            extracted_text = (analyze_image_text(content) or "").strip()
        except Exception:  # noqa: BLE001
            extracted_text = ""

    if not extracted_text:
        return jsonify({"tenant_id": tenant_id, "draft_items": [], "reason": "no_text_extracted"}), 200

    structured = analyze_text_structured(
        extracted_text,
        (
            "Extrae una lista de items de pedido en JSON con clave 'items'. "
            "Cada item debe incluir nombre, cantidad y precio si existe."
        ),
    )
    items = []
    if isinstance(structured, dict):
        maybe_items = structured.get("items") or structured.get("rows") or structured.get("productos") or []
        if isinstance(maybe_items, list):
            items = maybe_items

    catalog = CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant_id).all()
    catalog_by_name = {((c.nombre or "").strip().lower()): c for c in catalog}

    draft_items = []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("nombre") or item.get("producto") or "").strip()
        if not raw_name:
            continue
        name_key = raw_name.lower()
        match = catalog_by_name.get(name_key)
        if not match:
            # fuzzy contains fallback
            match = next((c for c in catalog if name_key in (c.nombre or "").lower()), None)

        qty = item.get("cantidad") or item.get("qty") or 1
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = 1.0

        draft_items.append(
            {
                "input_name": raw_name,
                "cantidad": qty,
                "catalogo_item_id": match.id if match else None,
                "catalog_name": match.nombre if match else None,
                "match_status": "matched" if match else "unmatched",
            }
        )

    matched_count = sum(1 for row in draft_items if row.get("match_status") == "matched")
    return jsonify(
        {
            "tenant_id": tenant_id,
            "source_text_preview": extracted_text[:500],
            "draft_items": draft_items,
            "matched_count": matched_count,
            "unmatched_count": max(len(draft_items) - matched_count, 0),
        }
    )
