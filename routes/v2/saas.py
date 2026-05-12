from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from extensions import db
from models import (
    CatalogoItem,
    EncEncuesta,
    EncRespuesta,
    MarketOrder,
    MunicipioTicket,
    Notification,
    NotificationTemplate,
    PublicSurvey,
    PublicSurveyResponse,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    User,
)
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.education_contracts import build_education_admin_menu, build_education_profile, is_education_tenant
from services.employee_routing import (
    build_employee_routing_payload,
    employee_ref,
    find_ticket_for_assignment,
    normalize_scope_list,
)
from services.catalog_quality import build_catalog_quality_payload
from services.operational_intelligence import build_operational_dashboard, build_operational_freshness
from services.v2.sla_service import is_ticket_overdue
from services.whatsapp_experience import build_whatsapp_experience
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_saas_bp = Blueprint("v2_saas", __name__, url_prefix="/api/v2")


_ACTIVE_TICKET_STATES = {"nuevo", "open", "pendiente", "in_progress", "en_proceso", "waiting_customer"}
_CLOSED_TICKET_STATES = {"resuelto", "cerrado", "closed", "resolved"}


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


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str):
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


def _tenant_slug_from_request(path_slug: str | None = None) -> str:
    return (
        path_slug
        or request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()


def _resolve_tenant_or_error(current_user: User, path_slug: str | None = None):
    slug = _tenant_slug_from_request(path_slug)
    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=slug or None)
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "send_valid_tenant")

    if not _user_can_access_tenant(current_user, tenant):
        return None, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")
    return tenant, None


def _user_can_access_tenant(user: User, tenant: TenantProfile) -> bool:
    role = str(getattr(user, "rol", "") or "").lower()
    if role == "super_admin":
        return True
    if str(getattr(user, "tenant_id", "") or "") == str(tenant.id):
        return True
    if (getattr(user, "tenant_slug", "") or "").strip().lower() == (tenant.slug or "").strip().lower():
        return True
    owner_ids = {getattr(tenant, "pyme_id", None), getattr(tenant, "municipio_id", None)}
    return getattr(user, "id", None) in owner_ids


def _tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "plan": tenant.plan,
        "is_active": bool(getattr(tenant, "is_active", True)),
    }


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _date_range_from_request(default_days: int = 30) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=default_days)
    end_date = now

    from_str = request.args.get("from")
    to_str = request.args.get("to")

    if from_str:
        try:
            start_date = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    if to_str:
        try:
            end_date = datetime.fromisoformat(to_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    return start_date, end_date


def _tenant_owner_ref(tenant: TenantProfile) -> dict[str, Any] | None:
    owner = tenant.pyme or tenant.municipio
    if not owner:
        return None
    return {
        "id": owner.id,
        "name": owner.name,
        "email": owner.email,
        "role": owner.rol,
        "tenant_slug": owner.tenant_slug,
    }


def _employee_scope(emp: User) -> dict[str, list[str]]:
    data = emp.accesibilidad if isinstance(emp.accesibilidad, dict) else {}
    scope = data.get("employee_scope") if isinstance(data.get("employee_scope"), dict) else {}
    return {
        "categorias": [str(v).strip() for v in scope.get("categorias", []) if str(v).strip()][:30],
        "zonas": [str(v).strip() for v in scope.get("zonas", []) if str(v).strip()][:30],
        "permisos": [str(v).strip() for v in scope.get("permisos", []) if str(v).strip()][:30],
        "channels": [str(v).strip() for v in scope.get("channels", []) if str(v).strip()][:30],
    }


def _ticket_extra(ticket: TenantTicket) -> dict[str, Any]:
    return ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}


def _ticket_status(ticket: TenantTicket) -> str:
    return str(ticket.estado or "").strip().lower()


def _ticket_channel(ticket: TenantTicket) -> str:
    extra = _ticket_extra(ticket)
    return str(extra.get("channel") or ticket.origen or "web").strip().lower()


def _open_tickets(tenant_id: int) -> list[TenantTicket]:
    tickets = TenantTicket.query.filter_by(tenant_id=tenant_id).all()
    return [ticket for ticket in tickets if _ticket_status(ticket) not in _CLOSED_TICKET_STATES]


def _employee_workload(tenant_id: int, employee_id: int) -> int:
    total = 0
    for ticket in _open_tickets(tenant_id):
        if str(_ticket_extra(ticket).get("assignee_id") or "") == str(employee_id):
            total += 1
    return total


def _coverage_items(tenant: TenantProfile) -> dict[str, Any]:
    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).order_by(User.id.asc()).all()
    open_tickets = _open_tickets(tenant.id)

    categories = sorted({str(ticket.categoria or "sin_categoria").strip().lower() for ticket in open_tickets})
    channels = sorted({_ticket_channel(ticket) for ticket in open_tickets})
    zones = sorted(
        {
            str(_ticket_extra(ticket).get("zone") or _ticket_extra(ticket).get("zona") or _ticket_extra(ticket).get("address") or "sin_zona")
            .strip()
            .lower()
            for ticket in open_tickets
        }
    )

    category_map: dict[str, list[dict[str, Any]]] = {category: [] for category in categories}
    zone_map: dict[str, list[dict[str, Any]]] = {zone: [] for zone in zones}
    channel_map: dict[str, list[dict[str, Any]]] = {channel: [] for channel in channels}
    employee_items = []

    for emp in employees:
        scope = _employee_scope(emp)
        coverage_score = 0
        if categories:
            coverage_score += len(set(scope["categorias"]) & set(categories)) / len(categories)
        if zones:
            coverage_score += len(set(scope["zonas"]) & set(zones)) / len(zones)
        if channels:
            coverage_score += len(set(scope["channels"]) & set(channels)) / len(channels)
        normalized_score = round(min(100.0, (coverage_score / max(1, bool(categories) + bool(zones) + bool(channels))) * 100), 2)

        ref = {"employee_id": emp.id, "name": emp.name, "email": emp.email}
        for category in scope["categorias"]:
            category_map.setdefault(category, []).append(ref)
        for zone in scope["zonas"]:
            zone_map.setdefault(zone, []).append(ref)
        for channel in scope["channels"]:
            channel_map.setdefault(channel, []).append(ref)

        employee_items.append(
            {
                "employee_id": emp.id,
                "id": emp.id,
                "name": emp.name,
                "email": emp.email,
                "roles": [getattr(emp, "rol", None)] if getattr(emp, "rol", None) else [],
                "scope": scope,
                "workload_open_tickets": _employee_workload(tenant.id, emp.id),
                "coverage_score": normalized_score,
            }
        )

    uncovered_categories = [key for key, value in category_map.items() if not value]
    uncovered_zones = [key for key, value in zone_map.items() if not value]
    uncovered_channels = [key for key, value in channel_map.items() if not value]
    total_dimensions = len(category_map) + len(zone_map) + len(channel_map)
    covered_dimensions = sum(1 for value in category_map.values() if value) + sum(1 for value in zone_map.values() if value) + sum(
        1 for value in channel_map.values() if value
    )
    coverage_rate = round((covered_dimensions / total_dimensions) * 100, 2) if total_dimensions else 100.0

    alerts = []
    if not employees:
        alerts.append({"severity": "high", "reason_code": "no_employees", "message": "No hay empleados activos para cubrir tickets."})
    if uncovered_categories:
        alerts.append({"severity": "medium", "reason_code": "uncovered_categories", "items": uncovered_categories})
    if uncovered_channels:
        alerts.append({"severity": "medium", "reason_code": "uncovered_channels", "items": uncovered_channels})

    return {
        "employees": employee_items,
        "coverage": {
            "categorias": category_map,
            "zonas": zone_map,
            "channels": channel_map,
            "uncovered_categories": uncovered_categories,
            "uncovered_zones": uncovered_zones,
            "uncovered_channels": uncovered_channels,
        },
        "summary": {
            "employees": len(employees),
            "open_tickets": len(open_tickets),
            "coverage_rate": coverage_rate,
            "covered_dimensions": covered_dimensions,
            "total_dimensions": total_dimensions,
            "alerts_count": len(alerts),
        },
        "alerts": alerts,
    }


def _notification_delivery_status(tenant_id: int, period_days: int = 7) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    rows = (
        db.session.query(Notification.channel, Notification.status, func.count(Notification.id))
        .filter(Notification.tenant_id == tenant_id)
        .filter(Notification.created_at >= since)
        .group_by(Notification.channel, Notification.status)
        .all()
    )
    by_channel: dict[str, dict[str, int]] = {}
    totals = {"queued": 0, "sent": 0, "failed": 0, "delayed": 0, "sending": 0}
    for channel, status, count in rows:
        channel_key = str(channel or "unknown")
        status_key = str(status or "unknown")
        amount = int(count or 0)
        by_channel.setdefault(channel_key, {"queued": 0, "sent": 0, "failed": 0, "delayed": 0, "sending": 0})
        by_channel[channel_key][status_key] = by_channel[channel_key].get(status_key, 0) + amount
        totals[status_key] = totals.get(status_key, 0) + amount

    delivered = totals.get("sent", 0)
    failed = totals.get("failed", 0)
    denominator = delivered + failed
    success_rate = round((delivered / denominator) * 100, 2) if denominator else 100.0
    return {
        "period_days": period_days,
        "since": since.isoformat(),
        "totals": {**totals, "success_rate": success_rate},
        "by_channel": by_channel,
    }


def _tenant_health_payload(tenant: TenantProfile) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    open_tickets = _open_tickets(tenant.id)
    overdue_tickets = [ticket for ticket in open_tickets if is_ticket_overdue(ticket) or _ticket_status(ticket) in {"vencido", "overdue"}]
    employees_count = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).count()
    notification_status = _notification_delivery_status(tenant.id)

    checks = {
        "active": bool(getattr(tenant, "is_active", True)),
        "widget_configured": bool(tenant.widget_config or tenant.widget_settings or cfg.get("widget_tokens")),
        "whatsapp_configured": bool(tenant.whatsapp_sender_id or cfg.get("whatsapp_sender_id")),
        "team_configured": employees_count > 0,
        "notifications_healthy": float((notification_status.get("totals") or {}).get("success_rate", 100.0)) >= 90.0,
        "sla_healthy": len(overdue_tickets) == 0,
    }
    score = round((sum(1 for ok in checks.values() if ok) / len(checks)) * 100, 2)
    status = "healthy" if score >= 85 else "warning" if score >= 60 else "critical"
    alerts = []
    for key, ok in checks.items():
        if not ok:
            alerts.append({"severity": "high" if key in {"active", "sla_healthy"} else "medium", "reason_code": key, "message": f"Check pendiente: {key}"})

    return {
        "contract_version": "tenant.health.v1",
        "tenant": _tenant_ref(tenant),
        "health": {
            "score": score,
            "status": status,
            "checks": checks,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        },
        "metrics": {
            "open_tickets": len(open_tickets),
            "overdue_tickets": len(overdue_tickets),
            "employees": employees_count,
            "notification_success_rate": notification_status["totals"]["success_rate"],
        },
        "integrations": {
            "widget": checks["widget_configured"],
            "whatsapp": checks["whatsapp_configured"],
            "payments": bool(cfg.get("mercadopago_access_token")),
            "notifications": checks["notifications_healthy"],
        },
        "queues": {
            "notifications_queued": notification_status["totals"].get("queued", 0),
            "notifications_delayed": notification_status["totals"].get("delayed", 0),
            "tickets_open": len(open_tickets),
        },
        "errors_recent": {
            "notifications_failed": notification_status["totals"].get("failed", 0),
            "tickets_overdue": len(overdue_tickets),
        },
        "alerts": alerts,
        "recommended_actions": [
            {"kind": alert["reason_code"], "priority": alert["severity"], "message": alert["message"]}
            for alert in alerts[:5]
        ],
    }


def _safe_count(query) -> int:
    try:
        return int(query.count() or 0)
    except Exception:
        return 0


def _survey_ops_summary(tenant: TenantProfile) -> dict[str, Any]:
    encuestas = EncEncuesta.query.filter_by(tenant_id=tenant.id).all()
    public_surveys = PublicSurvey.query.filter_by(tenant_id=tenant.id).all()
    public_survey_ids = [survey.id for survey in public_surveys]
    public_responses = 0
    if public_survey_ids:
        public_responses = _safe_count(PublicSurveyResponse.query.filter(PublicSurveyResponse.survey_id.in_(public_survey_ids)))
    legacy_responses = _safe_count(EncRespuesta.query.filter_by(tenant_id=tenant.id))
    live_votes = [
        encuesta
        for encuesta in encuestas
        if bool(getattr(encuesta, "es_votacion_envivo", False))
        or "vot" in str(getattr(encuesta, "tipo", "") or "").lower()
        or "vot" in str(getattr(encuesta, "titulo", "") or "").lower()
    ]
    active = [
        encuesta
        for encuesta in encuestas
        if str(getattr(encuesta, "estado", "") or "").lower() in {"publicada", "activa", "active", "published"}
    ]
    return {
        "contract_version": "tenant.surveys_ops.v1",
        "summary": {
            "surveys": len(encuestas),
            "public_surveys": len(public_surveys),
            "active": len(active),
            "live_votes": len(live_votes),
            "responses": legacy_responses + public_responses,
            "public_responses": public_responses,
        },
        "items": [
            {
                "id": encuesta.id,
                "slug": encuesta.slug,
                "title": encuesta.titulo,
                "type": encuesta.tipo,
                "status": encuesta.estado,
                "is_live_vote": bool(encuesta in live_votes),
                "show_live_results": bool(getattr(encuesta, "mostrar_resultados_envivo", False)),
            }
            for encuesta in encuestas[:12]
        ],
        "endpoints": {
            "admin": "/api/v2/surveys",
            "analytics": "/api/v2/analytics/operations/dashboard",
            "draft": "/api/v2/surveys/draft",
        },
    }


def _marketplace_ops_summary(tenant: TenantProfile) -> dict[str, Any]:
    products_query = CatalogoItem.query.filter_by(tenant_id=tenant.id)
    products_count = _safe_count(products_query)
    with_images = _safe_count(CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id, CatalogoItem.imagen_url.isnot(None)))
    quality = build_catalog_quality_payload(tenant, limit=8)
    quality_summary = quality.get("summary") or {}
    latest_imports = ((quality.get("imports") or {}).get("latest") or [])
    latest_import_status = (latest_imports[0] or {}).get("status") if latest_imports else None
    products_missing_images = max(0, products_count - with_images)
    try:
        orders_count = MarketOrder.legacy_safe_count(tenant_id=tenant.id)
        pending_orders = MarketOrder.legacy_safe_count(MarketOrder.status.in_(["pending", "created", "confirmed"]), tenant_id=tenant.id)
    except Exception:
        orders_count = 0
        pending_orders = 0

    return {
        "contract_version": "tenant.marketplace_ops.v1",
        "summary": {
            "products": products_count,
            "with_images": with_images,
            "missing_images": products_missing_images,
            "products_without_image": products_missing_images,
            "products_with_images": with_images,
            "products_missing_images": products_missing_images,
            "image_coverage_rate": round((with_images / products_count) * 100, 2) if products_count else 100.0,
            "ready_to_sell": quality_summary.get("ready_to_sell", 0),
            "missing_price": quality_summary.get("missing_price", 0),
            "missing_stock": quality_summary.get("missing_stock", 0),
            "ready_rate": quality_summary.get("ready_rate", 100.0),
            "orders": orders_count,
            "pending_orders": pending_orders,
            "bulk_import_status": latest_import_status or "idle",
        },
        "quality": {
            "contract_version": quality.get("contract_version"),
            "summary": quality_summary,
            "queues": quality.get("queues"),
            "endpoint": "/api/v2/catalog/quality",
        },
        "media_capabilities": {
            "product_images": True,
            "product_gallery": True,
            "bulk_import": ["csv", "xlsx", "txt", "pdf"],
            "image_extraction_from_import": True,
            "manual_image_upload": True,
            "pdf_catalog_generation": True,
        },
        "endpoints": {
            "items": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "catalog": f"/api/admin/tenants/{tenant.slug}/catalog",
            "catalog_quality": "/api/v2/catalog/quality",
            "bulk_import": "/api/admin/catalogo/importar",
            "bulk_import_v2": "/api/admin/catalog/import",
            "orders": f"/api/admin/tenants/{tenant.slug}/orders",
        },
    }


def _ticket_item_from_tenant(ticket: TenantTicket, source: str = "tenant_ticket") -> dict[str, Any]:
    extra = _ticket_extra(ticket)
    status = ticket.estado
    intent = extra.get("intent") or extra.get("action") or extra.get("lead_intent") or ticket.categoria
    return {
        "source": source,
        "id": ticket.id,
        "ticket_id": ticket.id,
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "status": status,
        "stage": extra.get("lead_stage") or status,
        "channel": _ticket_channel(ticket),
        "category": ticket.categoria,
        "intent": intent,
        "next_action": extra.get("next_action") or ("assign_or_reply" if str(status or "").lower() not in _CLOSED_TICKET_STATES else "view_history"),
        "origin": ticket.origen,
        "contact": extra.get("contact") if isinstance(extra.get("contact"), dict) else {},
        "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": extra.get("address")},
        "created_at": _iso(ticket.created_at),
        "updated_at": _iso(ticket.updated_at),
    }


def _ticket_item_from_legacy(ticket: Any, source: str) -> dict[str, Any]:
    created_at = getattr(ticket, "fecha", None)
    updated_at = getattr(ticket, "ultima_actividad", None) or created_at
    status = getattr(ticket, "estado", None)
    category = getattr(ticket, "categoria", None)
    ticket_id = getattr(ticket, "id", None)
    return {
        "source": source,
        "id": ticket_id,
        "ticket_id": ticket_id,
        "title": getattr(ticket, "asunto", None) or category or f"Ticket {getattr(ticket, 'id', '')}",
        "status": status,
        "stage": status,
        "channel": getattr(ticket, "canal_ingreso", None) or "web",
        "category": category,
        "intent": category or source,
        "next_action": "assign_or_reply" if str(status or "").lower() not in _CLOSED_TICKET_STATES else "view_history",
        "origin": source,
        "contact": {
            "name": getattr(ticket, "nombre_vecino", None) or getattr(ticket, "nombre_cliente", None),
            "phone": getattr(ticket, "telefono", None),
            "email": getattr(ticket, "email", None),
        },
        "location": {
            "lat": getattr(ticket, "latitud", None),
            "lng": getattr(ticket, "longitud", None),
            "address": getattr(ticket, "direccion", None),
        },
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
    }


def _tenant_lead_capture_summary(tenant: TenantProfile, limit: int = 20) -> dict[str, Any]:
    tenant_rows = TenantTicket.query.filter_by(tenant_id=tenant.id).order_by(TenantTicket.updated_at.desc()).limit(limit).all()
    municipio_rows = MunicipioTicket.query.filter_by(tenant_id=tenant.id).order_by(MunicipioTicket.fecha.desc()).limit(limit).all()
    pyme_rows = PymeTicket.query.filter_by(tenant_id=tenant.id).order_by(PymeTicket.fecha.desc()).limit(limit).all()

    items = [_ticket_item_from_tenant(ticket) for ticket in tenant_rows]
    items.extend(_ticket_item_from_legacy(ticket, "municipio_ticket") for ticket in municipio_rows)
    items.extend(_ticket_item_from_legacy(ticket, "pyme_ticket") for ticket in pyme_rows)
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    items = items[:limit]

    open_items = [item for item in items if str(item.get("status") or "").lower() not in _CLOSED_TICKET_STATES]
    demo_items = [
        item
        for item in items
        if str(item.get("origin") or "").lower() in {"demo", "landing", "widget", "pwa"}
        or str(item.get("channel") or "").lower() in {"widget", "web", "landing"}
    ]
    return {
        "contract_version": "tenant.lead_capture.v1",
        "summary": {
            "total_recent": len(items),
            "open": len(open_items),
            "demo_or_widget": len(demo_items),
            "channels": sorted({str(item.get("channel") or "unknown") for item in items}),
        },
        "items": items,
        "endpoints": {
            "tenant_leads": f"/api/admin/tenants/{tenant.slug}/leads",
            "omnichannel_inbox": "/api/v2/inbox/omnichannel",
            "tickets": "/api/v2/tickets",
        },
    }


def _tenant_readiness_payload(tenant: TenantProfile, *, marketplace: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    market_summary = marketplace.get("summary") or {}
    checks = {
        "profile": bool(tenant.nombre and tenant.slug and tenant.tipo),
        "branding": bool(tenant.logo_url or tenant.tema or tenant.theme_json),
        "widget": bool(tenant.widget_settings or tenant.widget_config or cfg.get("widget_tokens")),
        "whatsapp": bool(tenant.whatsapp_sender_id or cfg.get("whatsapp_sender_id")),
        "team": User.query.filter_by(tenant_id=tenant.id, es_empleado=True).count() > 0,
        "catalog": int(market_summary.get("products") or 0) > 0,
        "surveys": EncEncuesta.query.filter_by(tenant_id=tenant.id).count() > 0 or PublicSurvey.query.filter_by(tenant_id=tenant.id).count() > 0,
        "sla": int((health.get("metrics") or {}).get("overdue_tickets") or 0) == 0,
    }
    completed = sum(1 for ok in checks.values() if ok)
    score = round((completed / len(checks)) * 100, 2)
    return {
        "contract_version": "tenant.readiness.v1",
        "score": score,
        "completed": completed,
        "total": len(checks),
        "checks": checks,
        "missing": [key for key, ok in checks.items() if not ok],
    }


def _admin_modules_payload(tenant: TenantProfile, *, education_profile: dict[str, Any]) -> list[dict[str, Any]]:
    base = f"/t/{tenant.slug}"
    modules = [
        {
            "id": "profile",
            "label": "Perfil operativo",
            "route": f"{base}/profile",
            "endpoint": "/api/v2/tenant/admin-experience",
            "widgets": ["readiness", "branding", "capabilities", "integrations"],
        },
        {
            "id": "inbox",
            "label": "Inbox omnicanal",
            "route": f"{base}/inbox",
            "endpoint": "/api/v2/inbox/omnichannel",
            "widgets": ["tickets", "timeline", "presence", "handoff"],
        },
        {
            "id": "analytics",
            "label": "Metricas y mapas",
            "route": f"{base}/analytics",
            "endpoint": "/api/v2/analytics/operations/dashboard",
            "secondary_endpoints": ["/api/v2/analytics/operations/heatmap", "/api/v2/analytics/operations/freshness"],
            "widgets": ["kpis", "heatmap", "trends", "action_center"],
        },
        {
            "id": "surveys_votings",
            "label": "Encuestas y votaciones",
            "route": f"{base}/surveys",
            "endpoint": "/api/v2/surveys",
            "secondary_endpoints": ["/api/v2/surveys/draft"],
            "widgets": ["public_surveys", "live_votes", "responses", "comments"],
        },
        {
            "id": "employees",
            "label": "Equipo y cobertura",
            "route": f"{base}/employees",
            "endpoint": "/api/v2/employee-coverage",
            "secondary_endpoints": ["/api/v2/employee-routing", "/api/v2/employees/{employee_id}/routing-scope"],
            "widgets": ["coverage", "workload", "routing_rules", "assignment"],
        },
        {
            "id": "marketplace",
            "label": "Marketplace y catalogo",
            "route": f"{base}/marketplace",
            "endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "secondary_endpoints": ["/api/v2/catalog/quality", "/api/admin/catalogo/importar", "/api/admin/catalog/import", f"/api/admin/tenants/{tenant.slug}/orders"],
            "widgets": ["catalog_quality", "bulk_import", "image_coverage", "orders", "pdf_catalog"],
        },
        {
            "id": "widget_whatsapp",
            "label": "Widget, WhatsApp y voz",
            "route": f"{base}/channels",
            "endpoint": "/api/v2/whatsapp/experience",
            "secondary_endpoints": [
                f"/api/public/tenants/{tenant.slug}/widget-config",
                "/api/public/realtime/voice-capabilities",
                "/api/v2/notifications/hooks",
            ],
            "widgets": ["channel_health", "quick_menu", "media_capabilities", "realtime_voice", "tracking", "notifications"],
        },
    ]
    if education_profile.get("is_education"):
        modules.append(
            {
                "id": "education",
                "label": "Operacion colegio",
                "route": f"{base}/educacion",
                "endpoint": "/api/v1/education/admin/menu",
                "secondary_endpoints": ["/api/v1/education/operations/summary", "/api/v1/education/cases?envelope=1"],
                "widgets": ["family_context", "school_cases", "attendance", "communications"],
            }
        )
    for module in modules:
        module.setdefault("secondary_endpoints", [])
        module.setdefault("widgets", [])
    return modules


def _build_tenant_admin_experience_payload(
    tenant: TenantProfile,
    *,
    start_date: datetime,
    end_date: datetime,
    app_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    health = _tenant_health_payload(tenant)
    dashboard = build_operational_dashboard(tenant, start_date, end_date)
    freshness = build_operational_freshness(tenant, start_date, end_date)
    marketplace = _marketplace_ops_summary(tenant)
    surveys = _survey_ops_summary(tenant)
    lead_capture = _tenant_lead_capture_summary(tenant)
    education_profile = build_education_profile(tenant)
    readiness = _tenant_readiness_payload(tenant, marketplace=marketplace, health=health)
    whatsapp = build_whatsapp_experience(tenant, app_config=app_config)
    employee_routing = build_employee_routing_payload(tenant)

    return {
        "contract_version": "tenant.admin_experience.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "tenant": _tenant_ref(tenant),
        "owner": _tenant_owner_ref(tenant),
        "profile": {
            "display_name": tenant.nombre,
            "status": "active" if getattr(tenant, "is_active", True) else "inactive",
            "tipo": tenant.tipo,
            "vertical": "educacion" if is_education_tenant(tenant) else (tenant.vertical or tenant.tipo),
            "subvertical": tenant.subvertical,
            "plan": tenant.plan,
            "domain": tenant.dominio,
            "logo_url": tenant.logo_url,
            "theme_config": tenant.get_theme_config() if hasattr(tenant, "get_theme_config") else {},
            "readiness": readiness,
        },
        "modules": _admin_modules_payload(tenant, education_profile=education_profile),
        "health": health,
        "operations": {
            "dashboard": dashboard,
            "freshness": freshness,
        },
        "lead_capture": lead_capture,
        "surveys_votings": surveys,
        "marketplace": marketplace,
        "employee_routing": {
            "contract_version": employee_routing.get("contract_version"),
            "summary": {
                "employees": len(employee_routing.get("employees") or []),
                "unassigned": ((employee_routing.get("queues") or {}).get("unassigned_count") or 0),
                "recommendations": len(employee_routing.get("recommendations") or []),
            },
            "endpoint": "/api/v2/employee-routing",
        },
        "whatsapp": {
            "contract_version": whatsapp.get("contract_version"),
            "channel": whatsapp.get("channel"),
            "conversation_intelligence": whatsapp.get("conversation_intelligence"),
            "tracking": whatsapp.get("tracking"),
            "content_modules": whatsapp.get("content_modules"),
            "endpoint": "/api/v2/whatsapp/experience",
        },
        "education": {
            "profile": education_profile,
            "admin_menu": build_education_admin_menu(tenant) if education_profile.get("is_education") else None,
        },
        "frontend_contract": {
            "render_as": "tenant_admin_operating_system",
            "primary_refresh_seconds": 30,
            "recommended_views": [
                "profile_header",
                "operations_summary",
                "inbox_board",
                "heatmap",
                "surveys_votings",
                "marketplace_catalog_quality",
                "whatsapp_operations_hub",
                "employee_coverage",
            ],
            "empty_state_behavior": "show_module_readiness_and_next_best_actions",
        },
    }


def _build_superadmin_command_center_payload(*, start_date: datetime, end_date: datetime, limit: int = 50) -> dict[str, Any]:
    tenants = TenantProfile.query.order_by(TenantProfile.created_at.desc()).limit(limit).all()
    tenant_items = []
    total_open = 0
    total_overdue = 0
    total_leads = 0
    total_health = 0.0

    for tenant in tenants:
        health = _tenant_health_payload(tenant)
        lead_capture = _tenant_lead_capture_summary(tenant, limit=5)
        readiness = _tenant_readiness_payload(tenant, marketplace=_marketplace_ops_summary(tenant), health=health)
        tenant_ref = _tenant_ref(tenant)
        health_block = health.get("health") or {}
        metrics = health.get("metrics") or {}
        health_score = float(health_block.get("score") or 0)
        risk_reason = "none"
        if int(metrics.get("overdue_tickets") or 0) > 0:
            risk_reason = "overdue_tickets"
        elif str(health_block.get("status") or "") in {"warning", "critical"}:
            risk_reason = "tenant_health_warning"
        elif readiness.get("missing"):
            risk_reason = "readiness_incomplete"
        total_open += int(metrics.get("open_tickets") or 0)
        total_overdue += int(metrics.get("overdue_tickets") or 0)
        total_leads += int((lead_capture.get("summary") or {}).get("open") or 0)
        total_health += health_score
        tenant_items.append(
            {
                "slug": tenant_ref.get("slug"),
                "tenant_slug": tenant_ref.get("slug"),
                "display_name": tenant_ref.get("nombre"),
                "tenant_name": tenant_ref.get("nombre"),
                "health_score": health_score,
                "status": health_block.get("status") or ("active" if tenant_ref.get("is_active") else "inactive"),
                "risk_reason": risk_reason,
                "tenant": tenant_ref,
                "owner": _tenant_owner_ref(tenant),
                "health": health_block,
                "metrics": metrics,
                "readiness": readiness,
                "lead_capture": lead_capture.get("summary"),
                "routes": {
                    "profile_360": f"/api/v2/tenants/{tenant.slug}/admin-experience",
                    "legacy_profile_360": f"/api/admin/tenants/{tenant.slug}/profile-360",
                    "impersonate": f"/api/admin/tenants/{tenant.slug}/impersonate",
                },
            }
        )

    risky = [
        item
        for item in tenant_items
        if str((item.get("health") or {}).get("status") or "") in {"warning", "critical"}
        or int((item.get("metrics") or {}).get("overdue_tickets") or 0) > 0
    ]
    risky.sort(key=lambda item: ((item.get("health") or {}).get("score") or 0, -int((item.get("metrics") or {}).get("overdue_tickets") or 0)))

    return {
        "contract_version": "superadmin.command_center.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "summary": {
            "tenants": len(tenant_items),
            "active_tenants": len([item for item in tenant_items if (item.get("tenant") or {}).get("is_active")]),
            "avg_health_score": round(total_health / len(tenant_items), 2) if tenant_items else 100.0,
            "open_tickets": total_open,
            "overdue_tickets": total_overdue,
            "open_leads": total_leads,
            "risky_tenants": len(risky),
        },
        "tenants": {
            "items": tenant_items,
            "top_risky": risky[:10],
        },
        "tenant_creation": {
            "endpoint": "/api/admin/tenants",
            "method": "POST",
            "required_fields": ["nombre", "tipo"],
            "optional_fields": ["slug", "plan", "owner_email", "vertical", "subvertical"],
            "supported_types": ["pyme", "municipio"],
            "supported_verticals": ["empresas", "gobierno", "educacion"],
        },
        "lead_capture": {
            "endpoint": "/api/admin/leads/strategic-overview",
            "recent_by_tenant": [
                {"tenant": item["tenant"], "summary": item.get("lead_capture") or {}}
                for item in tenant_items[:20]
            ],
        },
        "recommended_actions": [
            {
                "kind": "review_risky_tenants",
                "priority": "high" if total_overdue else "medium",
                "message": "Revisar tenants con health bajo, SLA vencido o readiness incompleta.",
            },
            {
                "kind": "standardize_profile_modules",
                "priority": "medium",
                "message": "Usar admin-experience como fuente unica para perfil, panel operativo y modulos.",
            },
        ],
        "frontend_contract": {
            "render_as": "superadmin_command_center",
            "primary_refresh_seconds": 60,
            "recommended_views": ["tenant_grid", "health_ranking", "lead_pipeline", "tenant_creation", "profile_360_drawer"],
            "drilldown_endpoint_template": "/api/v2/tenants/{tenant_slug}/admin-experience",
        },
    }


def _templates_payload(tenant_id: int) -> list[dict[str, Any]]:
    rows = NotificationTemplate.query.filter_by(tenant_id=tenant_id).order_by(NotificationTemplate.created_at.desc()).all()
    return [
        {
            "id": item.id,
            "key": item.key,
            "channel": item.channel,
            "subject_template": item.subject_template,
            "body_template": item.body_template,
            "is_active": bool(item.is_active),
            "quiet_hours_start": item.quiet_hours_start,
            "quiet_hours_end": item.quiet_hours_end,
            "metadata": item.metadata_json if isinstance(item.metadata_json, dict) else {},
        }
        for item in rows
    ]


@v2_saas_bp.route("/employee-coverage", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employee-coverage", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def employee_coverage_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    payload = _coverage_items(tenant)
    return _json_response({"contract_version": "employee.coverage.v1", "tenant": _tenant_ref(tenant), **payload})


@v2_saas_bp.route("/employee-routing", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employee-routing", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def employee_routing_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_employee_routing_payload(tenant))


@v2_saas_bp.route("/employees/<int:employee_id>/routing-scope", methods=["PATCH", "POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employees/<int:employee_id>/routing-scope", methods=["PATCH", "POST"])
@token_requerido
@require_role("admin", "super_admin")
def update_employee_routing_scope_v2(current_user, employee_id: int, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    employee = User.query.filter_by(id=employee_id, tenant_id=tenant.id, es_empleado=True).first()
    if not employee:
        return _error_response("Empleado no encontrado para este tenant", 404, "employee_not_found", "choose_valid_employee")

    payload = request.get_json(silent=True) or {}
    raw_scope = payload.get("employee_scope") if isinstance(payload.get("employee_scope"), dict) else payload
    categorias = normalize_scope_list(raw_scope.get("categorias") or raw_scope.get("categories"))
    zonas = normalize_scope_list(raw_scope.get("zonas") or raw_scope.get("zones"))
    channels = normalize_scope_list(raw_scope.get("channels") or raw_scope.get("canales"))
    permisos = normalize_scope_list(raw_scope.get("permisos") or raw_scope.get("permissions"))

    data = deepcopy(employee.accesibilidad) if isinstance(employee.accesibilidad, dict) else {}
    data["employee_scope"] = {
        "categorias": categorias,
        "zonas": zonas,
        "channels": channels,
        "permisos": permisos,
    }
    employee.accesibilidad = data
    employee.categorias_lista = categorias
    flag_modified(employee, "accesibilidad")
    db.session.add(employee)
    db.session.commit()

    return _json_response(
        {
            "contract_version": "employee.routing_scope.v1",
            "tenant": _tenant_ref(tenant),
            "employee": employee_ref(employee),
        }
    )


def _apply_employee_assignment(ticket: Any, assignee: User, actor: User) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if isinstance(ticket, TenantTicket):
        extra = dict(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
        extra["assignee_id"] = assignee.id
        extra["assignee_name"] = assignee.name
        extra["assignee_email"] = assignee.email
        _append_ticket_event(extra, action="assign", actor=actor, body=f"Asignado a {assignee.name}")
        ticket.datos_extra = extra
        ticket.updated_at = now
        flag_modified(ticket, "datos_extra")
    elif isinstance(ticket, (MunicipioTicket, PymeTicket)):
        ticket.asignado_a_id = assignee.id
        ticket.asignado_en = now
        if isinstance(ticket, MunicipioTicket):
            ticket.ultima_actividad = now
    db.session.add(ticket)
    return {"assignee_id": assignee.id, "assignee_name": assignee.name, "assigned_at": now.isoformat()}


@v2_saas_bp.route("/employee-routing/auto-assign", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employee-routing/auto-assign", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def employee_routing_auto_assign_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True) is not False
    limit = max(1, min(int(payload.get("limit", 25) or 25), 100))
    routing = build_employee_routing_payload(tenant)
    recommendations = routing.get("recommendations") or []
    explicit_tickets = payload.get("tickets") if isinstance(payload.get("tickets"), list) else []
    if explicit_tickets:
        wanted = {
            (str(item.get("source_model") or ""), int(item.get("id") or item.get("ticket_id") or 0))
            for item in explicit_tickets
            if str(item.get("source_model") or "") and str(item.get("id") or item.get("ticket_id") or "").isdigit()
        }
        recommendations = [
            item
            for item in recommendations
            if (
                str((item.get("ticket") or {}).get("source_model") or ""),
                int((item.get("ticket") or {}).get("id") or 0),
            )
            in wanted
        ]

    results = []
    for item in recommendations[:limit]:
        ticket_ref = item.get("ticket") or {}
        assignee_ref = item.get("suggested_assignee") or {}
        assignee_id = assignee_ref.get("id") or assignee_ref.get("employee_id")
        ticket_id = ticket_ref.get("id") or ticket_ref.get("ticket_id")
        source_model = str(ticket_ref.get("source_model") or "")
        applied = False
        assignment = None
        if assignee_id and ticket_id and not dry_run:
            assignee = User.query.filter_by(id=int(assignee_id), tenant_id=tenant.id, es_empleado=True).first()
            ticket = find_ticket_for_assignment(tenant, source_model, int(ticket_id))
            if assignee and ticket:
                assignment = _apply_employee_assignment(ticket, assignee, current_user)
                applied = True
        results.append({**item, "applied": applied, "assignment": assignment})

    if not dry_run:
        db.session.commit()

    return _json_response(
        {
            "contract_version": "employee.routing.auto_assign.v1",
            "tenant": _tenant_ref(tenant),
            "dry_run": dry_run,
            "applied_count": len([item for item in results if item.get("applied")]),
            "items": results,
        }
    )


@v2_saas_bp.route("/catalog/quality", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/catalog/quality", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def catalog_quality_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    limit = max(1, min(int(request.args.get("limit", 20) or 20), 100))
    return _json_response(build_catalog_quality_payload(tenant, limit=limit))


@v2_saas_bp.route("/tenant-health", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/health", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_health_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(_tenant_health_payload(tenant))


@v2_saas_bp.route("/tenant/admin-experience", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/admin-experience", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_admin_experience_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    start_date, end_date = _date_range_from_request(default_days=30)
    payload = _build_tenant_admin_experience_payload(
        tenant,
        start_date=start_date,
        end_date=end_date,
        app_config=current_app.config,
    )
    return _json_response(payload)


@v2_saas_bp.route("/whatsapp/experience", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/experience", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_experience_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_whatsapp_experience(tenant, app_config=current_app.config))


@v2_saas_bp.route("/superadmin/executive-summary", methods=["GET"])
@v2_saas_bp.route("/super-admin/executive-summary", methods=["GET"])
@token_requerido
@require_role("super_admin")
def executive_summary_v2(current_user):
    tenants = TenantProfile.query.order_by(TenantProfile.id.asc()).all()
    tenant_health = [_tenant_health_payload(tenant) for tenant in tenants]
    open_tickets = sum((item.get("metrics") or {}).get("open_tickets", 0) for item in tenant_health)
    overdue_tickets = sum((item.get("metrics") or {}).get("overdue_tickets", 0) for item in tenant_health)
    avg_health = round(sum((item.get("health") or {}).get("score", 0) for item in tenant_health) / len(tenant_health), 2) if tenant_health else 100.0
    risky = sorted(tenant_health, key=lambda item: (item["health"]["score"], item["metrics"]["overdue_tickets"]), reverse=False)[:10]

    return _json_response(
        {
            "contract_version": "superadmin.executive_summary.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "tenants": len(tenants),
                "active_tenants": sum(1 for tenant in tenants if getattr(tenant, "is_active", True)),
                "avg_health_score": avg_health,
                "open_tickets": open_tickets,
                "overdue_tickets": overdue_tickets,
                "risky_tenants": len([item for item in tenant_health if item["health"]["status"] != "healthy"]),
            },
            "tenant_health": {
                "items": [
                    {
                        "tenant": item["tenant"],
                        "health": item["health"],
                        "metrics": item["metrics"],
                        "alerts": item["alerts"],
                    }
                    for item in tenant_health
                ],
                "top_risky": [
                    {"tenant": item["tenant"], "health": item["health"], "metrics": item["metrics"]}
                    for item in risky
                ],
            },
            "recommended_actions": [
                {
                    "kind": "review_risky_tenants",
                    "priority": "high" if overdue_tickets else "medium",
                    "message": "Revisar tenants con health bajo, tickets vencidos o integraciones incompletas.",
                }
            ]
            if tenants
            else [],
        }
    )


@v2_saas_bp.route("/superadmin/command-center", methods=["GET"])
@v2_saas_bp.route("/super-admin/command-center", methods=["GET"])
@token_requerido
@require_role("super_admin")
def superadmin_command_center_v2(current_user):
    start_date, end_date = _date_range_from_request(default_days=30)
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    payload = _build_superadmin_command_center_payload(start_date=start_date, end_date=end_date, limit=limit)
    return _json_response(payload)


@v2_saas_bp.route("/notifications/hooks", methods=["GET", "POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def notification_hooks_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    hooks = cfg.get("notification_hooks") if isinstance(cfg.get("notification_hooks"), dict) else {}

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        hooks = {
            "preferences": payload.get("preferences") if isinstance(payload.get("preferences"), dict) else hooks.get("preferences", {}),
            "triggers": payload.get("triggers") if isinstance(payload.get("triggers"), list) else hooks.get("triggers", []),
            "delivery": payload.get("delivery") if isinstance(payload.get("delivery"), dict) else hooks.get("delivery", {}),
        }
        cfg["notification_hooks"] = hooks
        tenant.configuracion = cfg
        db.session.add(tenant)
        db.session.commit()

    delivery_status = _notification_delivery_status(tenant.id, period_days=max(1, min(int(request.args.get("period_days", 7) or 7), 90)))
    return _json_response(
        {
            "contract_version": "notifications.hooks.v1",
            "tenant": _tenant_ref(tenant),
            "preferences": hooks.get("preferences", {}),
            "triggers": hooks.get("triggers", []),
            "delivery": hooks.get("delivery", {}),
            "templates": _templates_payload(tenant.id),
            "delivery_status": delivery_status,
        }
    )


@v2_saas_bp.route("/notifications/delivery-status", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def notification_delivery_status_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    period_days = max(1, min(int(request.args.get("period_days", 7) or 7), 90))
    return _json_response(
        {
            "contract_version": "notifications.delivery_status.v1",
            "tenant": _tenant_ref(tenant),
            **_notification_delivery_status(tenant.id, period_days=period_days),
        }
    )


@v2_saas_bp.route("/inbox/omnichannel", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def omnichannel_inbox_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    tickets = TenantTicket.query.filter_by(tenant_id=tenant.id).order_by(TenantTicket.updated_at.desc()).limit(limit).all()
    items = []
    for ticket in tickets:
        extra = _ticket_extra(ticket)
        comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
        timeline = []
        for comment in comments[-20:]:
            if not isinstance(comment, dict):
                continue
            origin = comment.get("origin") or ("admin_panel" if comment.get("visibility") == "internal" else "public_tracking")
            timeline.append(
                {
                    "id": comment.get("id"),
                    "origin": origin,
                    "body": comment.get("body") or "",
                    "visibility": comment.get("visibility") or "public",
                    "created_at": comment.get("created_at"),
                }
            )
        items.append(
            {
                "id": ticket.id,
                "ticket_id": ticket.id,
                "conversation_id": extra.get("conversation_id") or f"ticket-{ticket.id}",
                "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
                "status": ticket.estado,
                "priority": extra.get("priority") or "medium",
                "channel": _ticket_channel(ticket),
                "category": ticket.categoria,
                "assignee": {"id": extra.get("assignee_id"), "name": None} if extra.get("assignee_id") else None,
                "contact": extra.get("contact") if isinstance(extra.get("contact"), dict) else {},
                "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": extra.get("address")},
                "timeline": timeline,
                "presence": {"viewers": [], "locked_by": None},
                "actions": ["assign", "reply", "handoff", "close"],
                "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
            }
        )

    return _json_response(
        {
            "contract_version": "inbox.omnichannel.v1",
            "tenant": _tenant_ref(tenant),
            "items": items,
            "summary": {
                "total": len(items),
                "open": len([item for item in items if str(item.get("status") or "").lower() not in _CLOSED_TICKET_STATES]),
                "unassigned": len([item for item in items if not item.get("assignee")]),
            },
        }
    )


def _inbox_ticket_payload(ticket: TenantTicket) -> dict[str, Any]:
    extra = _ticket_extra(ticket)
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    timeline = []
    for comment in comments[-30:]:
        if not isinstance(comment, dict):
            continue
        origin = comment.get("origin") or ("admin_panel" if comment.get("visibility") == "internal" else "public_tracking")
        timeline.append(
            {
                "id": comment.get("id"),
                "origin": origin,
                "body": comment.get("body") or "",
                "visibility": comment.get("visibility") or "public",
                "created_at": comment.get("created_at"),
                "actor": comment.get("actor") if isinstance(comment.get("actor"), dict) else None,
                "action": comment.get("action"),
            }
        )

    assignee = None
    if extra.get("assignee_id"):
        assignee = {
            "id": extra.get("assignee_id"),
            "name": extra.get("assignee_name"),
            "email": extra.get("assignee_email"),
        }

    return {
        "id": ticket.id,
        "ticket_id": ticket.id,
        "conversation_id": extra.get("conversation_id") or f"ticket-{ticket.id}",
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "status": ticket.estado,
        "priority": extra.get("priority") or "medium",
        "channel": _ticket_channel(ticket),
        "category": ticket.categoria,
        "assignee": assignee,
        "contact": extra.get("contact") if isinstance(extra.get("contact"), dict) else {},
        "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": extra.get("address")},
        "timeline": timeline,
        "presence": extra.get("presence") if isinstance(extra.get("presence"), dict) else {"viewers": [], "locked_by": None},
        "actions": ["assign", "reply", "handoff", "close", "reopen"],
        "handoff": extra.get("handoff") if isinstance(extra.get("handoff"), dict) else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
    }


def _append_ticket_event(extra: dict[str, Any], *, action: str, actor: User, body: str, visibility: str = "internal") -> None:
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    comments.append(
        {
            "id": uuid.uuid4().hex,
            "origin": "admin_panel",
            "action": action,
            "body": body,
            "visibility": visibility,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "actor": {"id": actor.id, "name": actor.name, "role": actor.rol},
        }
    )
    extra["comments"] = comments[-100:]


@v2_saas_bp.route("/inbox/omnichannel/<int:ticket_id>/actions", methods=["POST"])
@v2_saas_bp.route("/inbox/omnichannel/actions", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def omnichannel_inbox_action_v2(current_user, ticket_id: int | None = None):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    resolved_ticket_id = ticket_id or payload.get("ticket_id") or payload.get("id")
    try:
        resolved_ticket_id = int(resolved_ticket_id)
    except (TypeError, ValueError):
        return _error_response("ticket_id es obligatorio", 400, "ticket_id_required", "send_ticket_id")

    ticket = TenantTicket.query.filter_by(id=resolved_ticket_id, tenant_id=tenant.id).first()
    if not ticket:
        return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")

    action = str(payload.get("action") or payload.get("type") or "").strip().lower()
    if action not in {"assign", "reply", "handoff", "close", "reopen", "set_priority"}:
        return _error_response("Accion de inbox no soportada", 400, "unsupported_inbox_action", "send_supported_action")

    extra = deepcopy(_ticket_extra(ticket))
    event_body = ""

    if action == "assign":
        assignee_id = payload.get("assignee_id") or payload.get("user_id")
        try:
            assignee_id = int(assignee_id)
        except (TypeError, ValueError):
            return _error_response("assignee_id es obligatorio", 400, "assignee_required", "send_assignee_id")
        assignee = User.query.filter_by(id=assignee_id, tenant_id=tenant.id).first()
        if not assignee:
            return _error_response("Empleado no encontrado para este tenant", 404, "assignee_not_found", "choose_valid_assignee")
        extra["assignee_id"] = assignee.id
        extra["assignee_name"] = assignee.name
        extra["assignee_email"] = assignee.email
        if ticket.estado in {"nuevo", "open"}:
            ticket.estado = "en_proceso"
        event_body = f"Asignado a {assignee.name}"

    elif action == "reply":
        body = str(payload.get("body") or payload.get("message") or "").strip()
        if not body:
            return _error_response("El mensaje no puede estar vacio", 400, "reply_body_required", "send_reply_body")
        visibility = str(payload.get("visibility") or "public").strip().lower()
        event_body = body
        _append_ticket_event(extra, action=action, actor=current_user, body=body, visibility=visibility)

    elif action == "handoff":
        channel = str(payload.get("channel") or payload.get("target_channel") or "operator").strip().lower()
        extra["handoff"] = {
            "channel": channel,
            "status": "requested",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "requested_by": {"id": current_user.id, "name": current_user.name},
            "reason": payload.get("reason"),
        }
        ticket.estado = "en_proceso"
        event_body = f"Handoff solicitado: {channel}"

    elif action == "close":
        ticket.estado = str(payload.get("status") or "cerrado").strip().lower() or "cerrado"
        extra["closed_at"] = datetime.now(timezone.utc).isoformat()
        extra["closed_by"] = {"id": current_user.id, "name": current_user.name}
        event_body = payload.get("body") or "Ticket cerrado"

    elif action == "reopen":
        ticket.estado = str(payload.get("status") or "nuevo").strip().lower() or "nuevo"
        extra["reopened_at"] = datetime.now(timezone.utc).isoformat()
        extra["reopened_by"] = {"id": current_user.id, "name": current_user.name}
        event_body = payload.get("body") or "Ticket reabierto"

    elif action == "set_priority":
        priority = str(payload.get("priority") or "").strip().lower()
        if not priority:
            return _error_response("priority es obligatorio", 400, "priority_required", "send_priority")
        extra["priority"] = priority
        event_body = f"Prioridad actualizada: {priority}"

    if action != "reply":
        _append_ticket_event(extra, action=action, actor=current_user, body=str(event_body or action), visibility="internal")

    ticket.datos_extra = extra
    flag_modified(ticket, "datos_extra")
    ticket.updated_at = datetime.now(timezone.utc)
    db.session.add(ticket)
    db.session.commit()

    return _json_response(
        {
            "ok": True,
            "contract_version": "inbox.omnichannel.action.v1",
            "tenant": _tenant_ref(tenant),
            "action": action,
            "ticket": _inbox_ticket_payload(ticket),
        }
    )
