"""Admin AI endpoints with tenant isolation."""

from __future__ import annotations

import json
import io
from collections import Counter
from dataclasses import replace
from datetime import datetime
from flask import Blueprint, abort, g, jsonify, request
import pdfplumber

from extensions import db
from models import CatalogoItem, MunicipioTicket, PymePedido, PymeTicket, TenantProfile, TenantTicket, TicketComentario, User
from services.analytics import get_summary
from services.analytics.filters import parse_filters
from services.analytics.rbac import (
    TENANT_NAMESPACE_OWNER,
    TENANT_NAMESPACE_PLATFORM,
    TENANT_NAMESPACE_PROFILE,
    require_access,
)
from services.ai_backoffice_summaries import generate_backoffice_analytics_summary, generate_backoffice_ticket_summary
from services.ai_provider_status import build_ai_provider_status
from services.employee_ticket_access import (
    employee_ticket_category_access_allows,
    employee_ticket_category_scope,
)
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    resolve_municipio_ticket_access_tenant,
    resolve_unique_tenant_for_owner,
)
from services.vision_fallback_service import analyze_image_text, analyze_text_structured
from utils.roles import ROLE_EMPLEADO, canonical_role

admin_ai_bp = Blueprint("admin_ai_bp", __name__, url_prefix="/admin")

_ALLOWED_ORDER_DRAFT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
_MAX_ORDER_DRAFT_BYTES = 5 * 1024 * 1024
_BOT_CONFIG_KEY = "bot_settings"
_ALLOWED_BOT_FALLBACK_BEHAVIORS = {"derivar_humano", "auto_reply", "silent"}
_MAX_BOT_NAME_LEN = 80
_MAX_BOT_TONE_LEN = 50
_MAX_BOT_SYSTEM_PROMPT_LEN = 5000
_EMPTY_EMPLOYEE_CATEGORY_FILTER = "__no_authorized_employee_category__"


def _parse_tenant_id(value):
    if value is None:
        abort(400, description="tenant_id is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be an integer")


def _ensure_tenant_bot_settings(tenant):
    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    raw_bot_settings = config.get(_BOT_CONFIG_KEY)
    bot_settings = raw_bot_settings if isinstance(raw_bot_settings, dict) else {}

    return {
        "name": bot_settings.get("name"),
        "tone": bot_settings.get("tone"),
        "system_prompt": bot_settings.get("system_prompt"),
        "fallback_behavior": bot_settings.get("fallback_behavior"),
        "branding": {
            "logo_url": tenant.logo_url,
            "primary_color": bot_settings.get("branding", {}).get("primary_color") if isinstance(bot_settings.get("branding"), dict) else None,
            "secondary_color": bot_settings.get("branding", {}).get("secondary_color") if isinstance(bot_settings.get("branding"), dict) else None,
        },
    }


def _validate_optional_string(payload: dict, key: str, max_len: int):
    if key not in payload:
        return None
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        abort(400, description=f"{key} must be a string")
    trimmed = value.strip()
    if len(trimmed) > max_len:
        abort(400, description=f"{key} too long")
    return trimmed or None


def _validated_branding(payload: dict):
    if payload is None:
        return None
    if not isinstance(payload, dict):
        abort(400, description="branding must be an object")

    allowed = {"logo_url", "primary_color", "secondary_color"}
    unknown = set(payload.keys()) - allowed
    if unknown:
        abort(400, description="branding contains unknown fields")

    branding = {}
    for field in allowed:
        if field in payload:
            value = payload.get(field)
            if value is not None and not isinstance(value, str):
                abort(400, description=f"branding.{field} must be a string")
            branding[field] = (value or "").strip() or None
    return branding


def _payload_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "si"}


def _parse_ai_enrichment_scope(payload: dict) -> str:
    raw_scope = payload.get("scope", "municipio")
    if not isinstance(raw_scope, str):
        abort(400, description="scope must be municipio|pyme|tenant")
    scope = raw_scope.strip().lower() or "municipio"
    if scope in {"crm", "v2", "tenant_ticket"}:
        scope = "tenant"
    if scope not in {"municipio", "pyme", "tenant"}:
        abort(400, description="scope must be municipio|pyme|tenant")
    return scope


def _parse_comments_limit(payload: dict, *, default: int = 40) -> int:
    if _payload_truthy(payload.get("exclude_comments")):
        return 0
    raw_limit = payload.get("comments_limit", default)
    try:
        return max(0, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        abort(400, description="comments_limit must be an integer")


def _reject_ai_enrichment_mutation_request(payload: dict) -> None:
    mutation_flags = ("apply", "persist", "mutate", "auto_update", "auto_apply")
    if any(_payload_truthy(payload.get(flag)) for flag in mutation_flags):
        abort(400, description="ai-enrichment is advisory-only; state mutation is not supported")

    mutation_fields = ("estado", "state", "new_state", "assigned_to", "asignado_a_id")
    if any(field in payload for field in mutation_fields):
        abort(400, description="ai-enrichment cannot receive operational mutation fields")


def _ticket_number_candidates(payload: dict) -> list[str]:
    raw_values = [
        payload.get("nro_ticket"),
        payload.get("ticket_number"),
        payload.get("ticketNumber"),
        payload.get("numero_ticket"),
        payload.get("numero"),
    ]
    seen: set[str] = set()
    candidates: list[str] = []

    def add(value) -> None:
        if value is None:
            return
        normalized = str(value).strip().upper()
        if not normalized:
            return
        normalized = normalized.lstrip("#").strip()
        variants = [normalized]
        for prefix in ("M-", "P-", "T-"):
            if normalized.startswith(prefix):
                variants.append(normalized[len(prefix) :].strip())
        if normalized.startswith("TICKET "):
            variants.append(normalized[7:].strip())
        for variant in variants:
            if variant and variant not in seen:
                seen.add(variant)
                candidates.append(variant)

    for raw in raw_values:
        add(raw)
    return candidates


def _resolve_ticket_for_ai(ticket_id: int, scope: str, payload: dict):
    if scope == "tenant":
        ticket = db.session.get(TenantTicket, ticket_id)
        if ticket:
            return ticket

        for candidate in _ticket_number_candidates(payload):
            try:
                candidate_id = int(candidate.removeprefix("T-"))
            except (TypeError, ValueError):
                continue
            ticket = db.session.get(TenantTicket, candidate_id)
            if ticket:
                return ticket

        abort(404, description="ticket not found")

    model = MunicipioTicket if scope == "municipio" else PymeTicket
    ticket = db.session.get(model, ticket_id)
    if ticket:
        return ticket

    for candidate in _ticket_number_candidates(payload):
        query_value = candidate
        if scope == "pyme":
            try:
                query_value = int(candidate)
            except (TypeError, ValueError):
                continue
        ticket = model.query.filter(model.nro_ticket == query_value).first()
        if ticket:
            return ticket

    abort(404, description="ticket not found")


def _resolve_ticket_access_tenant(ticket, scope: str):
    if scope == "municipio":
        try:
            tenant = resolve_municipio_ticket_access_tenant(ticket)
        except TicketTenantScopeError:
            abort(404, description="ticket not found")
        return str(tenant.id), TENANT_NAMESPACE_PROFILE, tenant

    tenant_id = getattr(ticket, "tenant_id", None)
    if tenant_id:
        tenant = db.session.get(TenantProfile, tenant_id)
        return str(tenant_id), TENANT_NAMESPACE_PROFILE, tenant

    owner_id = getattr(ticket, "rubro_id", None) or getattr(ticket, "pyme_id", None) or getattr(ticket, "user_id", None)
    if owner_id:
        try:
            resolution = resolve_unique_tenant_for_owner(owner_id)
        except TicketTenantScopeError:
            abort(404, description="ticket tenant not resolved")
        if resolution.status == "ambiguous":
            abort(404, description="ticket tenant not resolved")
        if resolution.status == "unique" and resolution.tenant is not None:
            tenant = resolution.tenant
            return str(tenant.id), TENANT_NAMESPACE_PROFILE, tenant
        return str(owner_id), TENANT_NAMESPACE_OWNER, None
    abort(404, description="ticket tenant not resolved")


def _orm_actor_for_viewer(viewer) -> User | None:
    actor = getattr(g, "viewer", None)
    if isinstance(actor, User):
        return actor
    viewer_id = getattr(viewer, "id", None)
    if isinstance(viewer_id, int) and viewer_id > 0:
        candidate = db.session.get(User, viewer_id)
        if isinstance(candidate, User):
            return candidate
    return None


def _require_employee_ticket_category_access(ticket, viewer) -> None:
    actor = _orm_actor_for_viewer(viewer)
    if actor is not None and not employee_ticket_category_access_allows(actor, ticket):
        # Do not disclose that a ticket exists outside the operator's queue.
        abort(404, description="ticket not found")


def _scope_analytics_filters_for_employee(filters, viewer):
    actor = _orm_actor_for_viewer(viewer)
    if actor is None or (
        canonical_role(getattr(actor, "rol", None)) != ROLE_EMPLEADO
        and not bool(getattr(actor, "es_empleado", False))
    ):
        return filters

    allowed = set(employee_ticket_category_scope(actor).names)
    requested = {
        str(category or "").strip().lower()
        for category in filters.categorias
        if str(category or "").strip()
    }
    effective = allowed if not requested else allowed & requested
    categories = tuple(sorted(effective)) or (_EMPTY_EMPLOYEE_CATEGORY_FILTER,)
    return replace(filters, categorias=categories)


def _tenant_ticket_comments(ticket: TenantTicket, limit: int) -> list[dict]:
    if not limit:
        return []
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    return [item for item in comments if isinstance(item, dict)][:limit]


@admin_ai_bp.get("/bot/settings")
def get_bot_settings():
    tenant_id = _parse_tenant_id(request.args.get("tenant_id"))
    require_access(
        str(tenant_id),
        "operador",
        tenant_namespace=TENANT_NAMESPACE_PROFILE,
    )

    tenant = TenantProfile.query.get(tenant_id)
    if not tenant:
        abort(404, description="tenant not found")

    return jsonify({"tenant_id": tenant.id, "settings": _ensure_tenant_bot_settings(tenant)})


@admin_ai_bp.put("/bot/settings")
def update_bot_settings():
    payload = request.get_json(silent=True) or {}
    tenant_id = _parse_tenant_id(payload.get("tenant_id"))
    require_access(
        str(tenant_id),
        "admin",
        required_capability="settings.tenant.write",
        tenant_namespace=TENANT_NAMESPACE_PROFILE,
    )

    tenant = TenantProfile.query.get(tenant_id)
    if not tenant:
        abort(404, description="tenant not found")

    allowed_top_level = {"tenant_id", "name", "tone", "system_prompt", "fallback_behavior", "branding"}
    unknown_fields = set(payload.keys()) - allowed_top_level
    if unknown_fields:
        abort(400, description="payload contains unknown fields")

    name = _validate_optional_string(payload, "name", _MAX_BOT_NAME_LEN)
    tone = _validate_optional_string(payload, "tone", _MAX_BOT_TONE_LEN)
    system_prompt = _validate_optional_string(payload, "system_prompt", _MAX_BOT_SYSTEM_PROMPT_LEN)

    fallback_behavior = None
    if "fallback_behavior" in payload:
        fallback_behavior = payload.get("fallback_behavior")
        if fallback_behavior is not None:
            if not isinstance(fallback_behavior, str):
                abort(400, description="fallback_behavior must be a string")
            fallback_behavior = fallback_behavior.strip().lower()
            if fallback_behavior not in _ALLOWED_BOT_FALLBACK_BEHAVIORS:
                abort(400, description="invalid fallback_behavior")

    branding = _validated_branding(payload.get("branding")) if "branding" in payload else None

    config = dict(tenant.configuracion) if isinstance(tenant.configuracion, dict) else {}
    current_bot_settings = config.get(_BOT_CONFIG_KEY)
    bot_settings = dict(current_bot_settings) if isinstance(current_bot_settings, dict) else {}

    if "name" in payload:
        bot_settings["name"] = name
    if "tone" in payload:
        bot_settings["tone"] = tone
    if "system_prompt" in payload:
        bot_settings["system_prompt"] = system_prompt
    if "fallback_behavior" in payload:
        bot_settings["fallback_behavior"] = fallback_behavior
    if branding is not None:
        bot_settings["branding"] = {
            "primary_color": branding.get("primary_color"),
            "secondary_color": branding.get("secondary_color"),
        }
        if "logo_url" in branding:
            tenant.logo_url = branding.get("logo_url")

    config[_BOT_CONFIG_KEY] = bot_settings
    tenant.configuracion = config
    db.session.commit()

    return jsonify({"tenant_id": tenant.id, "settings": _ensure_tenant_bot_settings(tenant)})


@admin_ai_bp.get("/ai/provider-status")
def ai_provider_status():
    require_access(
        "*",
        "admin",
        tenant_namespace=TENANT_NAMESPACE_PLATFORM,
    )
    include_smoke = str(request.args.get("smoke") or "").strip().lower() in {"1", "true", "yes", "on"}
    include_live = str(request.args.get("live") or "").strip().lower() in {"1", "true", "yes", "on"}
    return jsonify(build_ai_provider_status(include_smoke=include_smoke, include_live=include_live))


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
            "categoria": payload.get("categoria"),
        }
    )
    viewer = require_access(
        filters.tenant_id,
        "operador",
        tenant_namespace=TENANT_NAMESPACE_OWNER,
    )
    filters = _scope_analytics_filters_for_employee(filters, viewer)

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
        report = generate_backoffice_analytics_summary(metrics, tenant_type=filters.scope)

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

    ticket = _resolve_ticket_for_ai(ticket_id, scope, payload)
    access_tenant_id, access_namespace, _tenant = _resolve_ticket_access_tenant(ticket, scope)
    viewer = require_access(
        access_tenant_id,
        "operador",
        tenant_namespace=access_namespace,
    )
    _require_employee_ticket_category_access(ticket, viewer)

    comments = (
        TicketComentario.query.filter(
            TicketComentario.municipio_ticket_id == ticket.id
            if scope == "municipio"
            else TicketComentario.pyme_ticket_id == ticket.id
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

    summary = generate_backoffice_ticket_summary(ticket_payload)
    return jsonify({"ticket_id": ticket.id, "scope": scope, "ai": summary})


@admin_ai_bp.post("/tickets/<int:ticket_id>/ai-enrichment")
def ticket_ai_enrichment(ticket_id: int):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400, description="payload must be an object")
    _reject_ai_enrichment_mutation_request(payload)
    scope = _parse_ai_enrichment_scope(payload)
    comments_limit = _parse_comments_limit(payload)

    ticket = _resolve_ticket_for_ai(ticket_id, scope, payload)
    access_tenant_id, access_namespace, tenant = _resolve_ticket_access_tenant(ticket, scope)
    viewer = require_access(
        access_tenant_id,
        "operador",
        tenant_namespace=access_namespace,
    )
    _require_employee_ticket_category_access(ticket, viewer)

    if scope == "tenant":
        comments = _tenant_ticket_comments(ticket, comments_limit)
        enrichment_scope = "municipio" if str(getattr(tenant, "tipo", "") or "").lower() == "municipio" else "pyme"
    else:
        comments = (
            TicketComentario.query.filter(
                TicketComentario.municipio_ticket_id == ticket.id
                if scope == "municipio"
                else TicketComentario.pyme_ticket_id == ticket.id
            )
            .order_by(TicketComentario.fecha.asc())
            .limit(comments_limit)
            .all()
            if comments_limit
            else []
        )
        enrichment_scope = scope

    from services.ticket_ai_enrichment import build_ticket_ai_enrichment

    enrichment = build_ticket_ai_enrichment(
        ticket,
        scope=enrichment_scope,
        comments=comments,
        tenant=tenant,
    )
    if scope == "tenant":
        enrichment["ticket_type"] = "tenant"
        enrichment["domain_scope"] = enrichment_scope
    return jsonify(enrichment)


@admin_ai_bp.post("/ai/product-recommendations")
def product_recommendations():
    payload = request.get_json(silent=True) or {}
    tenant_id = _parse_tenant_id(payload.get("tenant_id"))

    require_access(
        str(tenant_id),
        "operador",
        tenant_namespace=TENANT_NAMESPACE_PROFILE,
    )
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

    require_access(
        str(tenant_id),
        "operador",
        tenant_namespace=TENANT_NAMESPACE_PROFILE,
    )

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
