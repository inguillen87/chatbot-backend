from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import quote_plus
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
    tenant_open_ticket_snapshots,
    tenant_operational_dimensions,
    workload_by_employee,
)
from services.catalog_quality import build_catalog_quality_fallback_payload, build_catalog_quality_payload
from services.demo_sandbox_contract import build_demo_whatsapp_sandbox_contract, sandbox_context_from_contract
from services.operational_intelligence import build_operational_dashboard, build_operational_freshness
from services.provider_platform import build_whatsapp_provider_status, sync_twilio_provider_records
from services.plan_access import integration_access_payload, integration_frontend_contract, plan_allows_full_integrations
from services.twilio_tech_provider import (
    build_twilio_tech_provider_contract,
    merge_twilio_state,
    poll_whatsapp_sender_status,
    provision_twilio_subaccount,
    provision_twilio_voice_application,
    register_whatsapp_sender,
)
from services.v2.sla_service import is_ticket_overdue
from services.whatsapp_experience import _template_creation_manifest_payload, build_whatsapp_experience
from utils.auth_helpers import token_requerido
from utils.permissions import require_role
from utils.roles import first_specific_tenant_slug, is_super_admin_role

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


def _integration_plan_error(tenant: TenantProfile, feature_id: str = "whatsapp_business_platform"):
    access = integration_access_payload(tenant)
    frontend_contract = integration_frontend_contract(access, feature_id)
    return _json_response(
        {
            **access,
            "access": access,
            "frontend_contract": frontend_contract,
            "frontend": frontend_contract,
            "contract_version": "tenant.integration_access.v1",
            "status_code": 403,
            "tenant": _tenant_ref(tenant),
            "retryable": False,
            "action_hint": "upgrade_to_full",
            "error": "plan_required",
            "error_detail": {"code": 403, "message": access["message"]},
        },
        403,
    )


def _require_full_integration_plan(tenant: TenantProfile, feature_id: str = "whatsapp_business_platform"):
    if plan_allows_full_integrations(tenant):
        return None
    return _integration_plan_error(tenant, feature_id)


def _tenant_slug_from_request(path_slug: str | None = None) -> str:
    body = request.get_json(silent=True) if request.method in {"POST", "PUT", "PATCH"} else None
    body_slug = body.get("tenant_slug") if isinstance(body, dict) else None
    return first_specific_tenant_slug(
        path_slug,
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
        body_slug,
    )


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
    if is_super_admin_role(role):
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
    ticket_snapshots = tenant_open_ticket_snapshots(tenant)
    workloads = workload_by_employee(tenant)
    supported_dimensions = tenant_operational_dimensions(tenant, ticket_snapshots)

    categories = sorted(set(supported_dimensions["categorias"]))
    channels = sorted(set(supported_dimensions["channels"]) | {item["channel"] for item in ticket_snapshots if item["channel"]})
    zones = sorted(set(supported_dimensions["zonas"]))

    category_map: dict[str, list[dict[str, Any]]] = {category: [] for category in categories}
    zone_map: dict[str, list[dict[str, Any]]] = {zone: [] for zone in zones}
    channel_map: dict[str, list[dict[str, Any]]] = {channel: [] for channel in channels}
    category_set = set(categories)
    zone_set = set(zones)
    channel_set = set(channels)
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
            if category_set and category not in category_set:
                continue
            category_map.setdefault(category, []).append(ref)
        for zone in scope["zonas"]:
            if zone_set and zone not in zone_set:
                continue
            zone_map.setdefault(zone, []).append(ref)
        for channel in scope["channels"]:
            if channel_set and channel not in channel_set:
                continue
            channel_map.setdefault(channel, []).append(ref)

        employee_items.append(
            {
                "employee_id": emp.id,
                "id": emp.id,
                "name": emp.name,
                "email": emp.email,
                "roles": [getattr(emp, "rol", None)] if getattr(emp, "rol", None) else [],
                "scope": scope,
                "workload_open_tickets": workloads.get(emp.id, 0),
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
            "dimension_sources": supported_dimensions.get("sources") or {},
            "uncovered_categories": uncovered_categories,
            "uncovered_zones": uncovered_zones,
            "uncovered_channels": uncovered_channels,
        },
        "summary": {
            "employees": len(employees),
            "open_tickets": len(ticket_snapshots),
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
            "low_stock": quality_summary.get("low_stock", 0),
            "out_of_stock": quality_summary.get("out_of_stock", 0),
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
        "inventory": quality.get("inventory"),
        "media_capabilities": {
            "product_images": True,
            "product_gallery": True,
            "bulk_import": ["csv", "xlsx", "txt", "pdf"],
            "stock_only_import": ["csv", "xlsx"],
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
            "stock_only_import_v2": "/api/admin/catalog/import",
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
            "id": "transactions",
            "label": "Transacciones",
            "route": f"{base}/transactions",
            "endpoint": "/api/v2/whatsapp/experience",
            "secondary_endpoints": [
                "/api/v2/payments/checkout-status",
                f"/api/v2/tenants/{tenant.slug}/payments/status",
                f"/api/v2/tenants/{tenant.slug}/payments/checkout-session",
            ],
            "widgets": ["finance_flows", "secure_checkout", "kyc", "collections", "signature", "audit_trail"],
        },
        {
            "id": "widget_whatsapp",
            "label": "Widget/WhatsApp/Voz",
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
        module.setdefault("audience", "admin")
        module.setdefault("secondary_endpoints", [])
        module.setdefault("widgets", [])
    return modules


def _admin_navigation_payload(tenant: TenantProfile, modules: list[dict[str, Any]]) -> dict[str, Any]:
    primary = [
        {
            "id": module.get("id"),
            "label": module.get("label"),
            "route": module.get("route"),
            "endpoint": module.get("endpoint"),
            "audience": module.get("audience") or "admin",
            "visible": True,
        }
        for module in modules
        if module.get("id") in {"profile", "inbox", "analytics", "surveys_votings", "employees", "marketplace", "transactions", "widget_whatsapp"}
    ]
    base = f"/t/{tenant.slug}"
    return {
        "contract_version": "tenant.admin_navigation.v1",
        "primary": primary,
        "quick_actions": [
            {
                "id": "open_operations",
                "label": "Tablero",
                "route": f"{base}/analytics",
                "endpoint": "/api/v2/analytics/operations/dashboard",
                "icon": "layout-dashboard",
                "visible": True,
            },
            {
                "id": "open_heatmap",
                "label": "Mapa",
                "route": f"{base}/analytics?view=heatmap",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "icon": "map",
                "visible": True,
            },
            {
                "id": "open_surveys",
                "label": "Encuestas",
                "route": f"{base}/surveys",
                "admin_route": f"/admin/encuestas?tenant_slug={tenant.slug}",
                "endpoint": "/api/v2/surveys",
                "icon": "clipboard-list",
                "visible": True,
            },
            {
                "id": "open_employees",
                "label": "Equipo",
                "route": f"{base}/employees",
                "endpoint": "/api/v2/employee-routing",
                "icon": "users",
                "visible": True,
            },
            {
                "id": "open_inbox",
                "label": "Reclamos",
                "route": f"{base}/inbox",
                "endpoint": "/api/v2/inbox/omnichannel",
                "icon": "inbox",
                "visible": True,
            },
        ],
        "inbox_tabs": [
            {"id": "open", "label": "Abiertos", "visible": True},
            {"id": "assigned", "label": "Asignados", "visible": True},
            {"id": "unassigned", "label": "Sin asignar", "visible": True},
            {"id": "resolved", "label": "Resueltos", "visible": True},
        ],
        "hide_legacy_tabs": ["workspace", "live_bridge", "templates"],
    }


def _admin_panel_widgets_payload(tenant: TenantProfile, *, dashboard: dict[str, Any], employee_routing: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": "tenant.admin_panel_widgets.v1",
        "layout": "operational_command_center",
        "hero_widgets": [
            {
                "id": "operations_summary",
                "label": "Resumen operativo",
                "render_as": "kpi_summary",
                "endpoint": "/api/v2/analytics/operations/dashboard",
                "data": dashboard.get("summary") or dashboard.get("metrics") or {},
                "empty_state": "Todavia no hay actividad suficiente para calcular el resumen.",
            },
            {
                "id": "heatmap_summary",
                "label": "Mapa de calor",
                "render_as": "heatmap_preview",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "can_render_heatmap": True,
                "empty_state": "Compartiendo ubicaciones o reclamos con direccion se activa este mapa.",
            },
            {
                "id": "location_widget",
                "label": "Ubicacion y cobertura",
                "render_as": "location_coverage",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "map_style_endpoint": "/api/map/config",
                "empty_state": "Configura zonas o genera reclamos con ubicacion para ver cobertura territorial.",
            },
            {
                "id": "employee_assignment",
                "label": "Equipo y asignacion",
                "render_as": "assignment_queue",
                "endpoint": "/api/v2/employee-routing",
                "summary": {
                    "employees": len(employee_routing.get("employees") or []),
                    "unassigned": ((employee_routing.get("queues") or {}).get("unassigned_count") or 0),
                    "categories": len((employee_routing.get("routing_rules") or {}).get("categories") or []),
                },
                "empty_state": "Carga empleados y reglas por categoria para asignar tickets sin ruido.",
            },
        ],
        "ticket_workspace": {
            "render_as": "focused_ticket_board",
            "primary_actions": ["review_unassigned", "auto_assign", "assign_by_category", "change_status", "handoff_human"],
            "employees_endpoint": "/api/v2/employee-routing",
            "routing_scope_endpoint": "/api/v2/employees/{employee_id}/routing-scope",
            "auto_assign_endpoint": "/api/v2/employee-routing/auto-assign",
            "recommended_filters": ["estado", "categoria", "asignado_a", "canal", "prioridad"],
        },
    }


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
    modules = _admin_modules_payload(tenant, education_profile=education_profile)

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
        "modules": modules,
        "navigation": _admin_navigation_payload(tenant, modules),
        "admin_panel_widgets": _admin_panel_widgets_payload(
            tenant,
            dashboard=dashboard,
            employee_routing=employee_routing,
        ),
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


def _ops_qa_check_result(
    *,
    check_id: str,
    label: str,
    ok: bool,
    status: str | None = None,
    severity: str = "warning",
    details: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    resolved_status = status or ("pass" if ok else severity)
    return {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "status": resolved_status,
        "severity": "info" if ok else severity,
        "endpoint": endpoint,
        "details": dict(details or {}),
        "next_action": next_action or ("continue" if ok else "review_configuration"),
    }


def _build_tenant_ops_qa_playbook(
    tenant: TenantProfile,
    *,
    start_date: datetime,
    end_date: datetime,
    app_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    admin = _build_tenant_admin_experience_payload(
        tenant,
        start_date=start_date,
        end_date=end_date,
        app_config=app_config,
    )
    modules = {str(item.get("id")): item for item in admin.get("modules", []) if isinstance(item, Mapping)}
    operations = admin.get("operations") if isinstance(admin.get("operations"), Mapping) else {}
    freshness = operations.get("freshness") if isinstance(operations.get("freshness"), Mapping) else {}
    freshness_summary = freshness.get("summary") if isinstance(freshness.get("summary"), Mapping) else {}
    lead_capture = admin.get("lead_capture") if isinstance(admin.get("lead_capture"), Mapping) else {}
    lead_summary = lead_capture.get("summary") if isinstance(lead_capture.get("summary"), Mapping) else {}
    marketplace = admin.get("marketplace") if isinstance(admin.get("marketplace"), Mapping) else {}
    market_summary = marketplace.get("summary") if isinstance(marketplace.get("summary"), Mapping) else {}
    surveys = admin.get("surveys_votings") if isinstance(admin.get("surveys_votings"), Mapping) else {}
    survey_summary = surveys.get("summary") if isinstance(surveys.get("summary"), Mapping) else {}
    employee_routing = build_employee_routing_payload(tenant)
    employee_summary = admin.get("employee_routing", {}).get("summary", {}) if isinstance(admin.get("employee_routing"), Mapping) else {}
    whatsapp = build_whatsapp_experience(tenant, app_config=app_config)
    whatsapp_templates = (whatsapp.get("template_blueprint") or {}).get("registry_summary") if isinstance(whatsapp.get("template_blueprint"), Mapping) else {}
    whatsapp_webviews = (whatsapp.get("webview_blueprint") or {}).get("summary") if isinstance(whatsapp.get("webview_blueprint"), Mapping) else {}
    finance_runtime = whatsapp.get("finance_transactional") if isinstance(whatsapp.get("finance_transactional"), Mapping) else {}
    finance_summary = finance_runtime.get("summary") if isinstance(finance_runtime.get("summary"), Mapping) else {}
    hf_runtime = (((whatsapp.get("conversation_intelligence") or {}).get("huggingface_ai") or {}) if isinstance(whatsapp.get("conversation_intelligence"), Mapping) else {})

    checks = [
        _ops_qa_check_result(
            check_id="admin_os_contract",
            label="Panel del tenant",
            ok=admin.get("contract_version") == "tenant.admin_experience.v1"
            and {"inbox", "analytics", "transactions", "widget_whatsapp"}.issubset(set(modules.keys())),
            severity="critical",
            endpoint="/api/v2/tenant/admin-experience",
            next_action="fix_admin_experience_contract",
            details={
                "contract_version": admin.get("contract_version"),
                "modules": sorted(modules.keys()),
                "render_as": (admin.get("frontend_contract") or {}).get("render_as"),
            },
        ),
        _ops_qa_check_result(
            check_id="claims_inbox",
            label="Reclamos, tickets e inbox omnicanal",
            ok=int(lead_summary.get("open") or 0) >= 0 and "inbox" in modules,
            severity="critical",
            endpoint="/api/v2/inbox/omnichannel",
            next_action="review_inbox_contract_and_ticket_sources",
            details={
                "open": lead_summary.get("open"),
                "recent": lead_summary.get("total_recent"),
                "channels": lead_summary.get("channels"),
                "sample_count": len(lead_capture.get("items") or []),
            },
        ),
        _ops_qa_check_result(
            check_id="catalog_orders",
            label="Catalogo, pedidos y checkout conversacional",
            ok=int(market_summary.get("products") or 0) > 0
            and (whatsapp.get("commerce") or {}).get("checkout_experience", {}).get("ready") is True,
            severity="critical",
            endpoint="/api/v2/catalog/quality",
            next_action="complete_catalog_images_prices_stock_and_checkout",
            details={
                "products": market_summary.get("products"),
                "ready_to_sell": market_summary.get("ready_to_sell"),
                "missing_images": market_summary.get("missing_images"),
                "orders": market_summary.get("orders"),
                "checkout_ready": (whatsapp.get("commerce") or {}).get("checkout_experience", {}).get("ready"),
            },
        ),
        _ops_qa_check_result(
            check_id="survey_live_vote",
            label="Encuestas, votaciones y resultados en vivo",
            ok=int(survey_summary.get("live_votes") or 0) > 0 or int(survey_summary.get("active") or 0) > 0,
            severity="warning",
            endpoint="/api/v2/surveys",
            next_action="publish_survey_or_live_vote",
            details={
                "surveys": survey_summary.get("surveys"),
                "active": survey_summary.get("active"),
                "live_votes": survey_summary.get("live_votes"),
                "responses": survey_summary.get("responses"),
            },
        ),
        _ops_qa_check_result(
            check_id="heatmap_analytics",
            label="Metricas, mapas y heatmap territorial",
            ok=bool(freshness_summary.get("can_render_heatmap")),
            severity="warning",
            endpoint="/api/v2/analytics/operations/heatmap",
            next_action="collect_locations_or_geocode_ticket_addresses",
            details={
                "freshness_status": freshness.get("status"),
                "can_render_heatmap": freshness_summary.get("can_render_heatmap"),
                "dashboard_contract": (operations.get("dashboard") or {}).get("contract_version") if isinstance(operations.get("dashboard"), Mapping) else None,
            },
        ),
        _ops_qa_check_result(
            check_id="employee_routing",
            label="Ruteo, asignacion y cobertura del equipo",
            ok=int(employee_summary.get("employees") or 0) > 0
            and employee_routing.get("contract_version") == "employee.routing.v1",
            severity="warning",
            endpoint="/api/v2/employee-routing",
            next_action="load_employees_and_category_scope",
            details={
                "contract_version": employee_routing.get("contract_version"),
                "employees": employee_summary.get("employees"),
                "unassigned": employee_summary.get("unassigned"),
                "recommendations": employee_summary.get("recommendations"),
            },
        ),
        _ops_qa_check_result(
            check_id="whatsapp_templates_webviews",
            label="Plantillas WhatsApp, botones y webviews",
            ok=whatsapp.get("contract_version") == "whatsapp.experience.v1"
            and int((whatsapp_templates or {}).get("operational_webviews") or 0) > 0
            and int((whatsapp_webviews or {}).get("flows_total") or 0) > 0,
            severity="critical",
            endpoint="/api/v2/whatsapp/experience",
            next_action="sync_twilio_templates_and_validate_signed_webviews",
            details={
                "contract_version": whatsapp.get("contract_version"),
                "operational_catalog_total": (whatsapp_templates or {}).get("operational_catalog_total"),
                "operational_webviews": (whatsapp_templates or {}).get("operational_webviews"),
                "webview_flows_total": (whatsapp_webviews or {}).get("flows_total"),
                "qa_scenarios": (whatsapp.get("qa_playbook") or {}).get("scenario_count") if isinstance(whatsapp.get("qa_playbook"), Mapping) else None,
            },
        ),
        _ops_qa_check_result(
            check_id="transactional_finance_flows",
            label="Transacciones in-chat: alta, KYC, cobranza, pago y firma",
            ok="transactions" in modules
            and int((whatsapp_webviews or {}).get("flows_total") or 0) >= 2
            and "financial_services" in (((whatsapp.get("template_blueprint") or {}).get("operational_template_groups") or {}) if isinstance(whatsapp.get("template_blueprint"), Mapping) else {}),
            severity="critical",
            endpoint="/api/v2/whatsapp/experience",
            next_action="run_finance_onboarding_collection_signature_smoke",
            details={
                "module": "transactions" if "transactions" in modules else None,
                "templates_group": "financial_services",
                "webview_flows": ["finance_onboarding_kyc", "finance_credit_collection_signature"],
                "finance_runtime": finance_runtime.get("contract_version"),
                "finance_journeys": finance_summary.get("journeys"),
                "finance_ready_journeys": finance_summary.get("ready_journeys"),
                "confirmation_policy": "server_to_server_webhook",
            },
        ),
        _ops_qa_check_result(
            check_id="ai_runtime_multimodal",
            label="IA multimodal y fallback controlado",
            ok=bool((hf_runtime or {}).get("enabled")) and not bool((hf_runtime or {}).get("quota_depleted")),
            severity="warning",
            endpoint="/api/v2/whatsapp/experience",
            next_action="review_huggingface_runtime_or_fallback_provider",
            details={
                "enabled": (hf_runtime or {}).get("enabled"),
                "runtime_status": (hf_runtime or {}).get("runtime_status"),
                "quota_depleted": (hf_runtime or {}).get("quota_depleted"),
                "fallback_behavior": (hf_runtime or {}).get("fallback_behavior"),
            },
        ),
    ]
    passed = sum(1 for item in checks if item.get("ok"))
    critical_failed = [item for item in checks if item.get("severity") == "critical" and not item.get("ok")]
    status = "pass" if passed == len(checks) else ("fail" if critical_failed else "warning")
    e2e_readiness = _build_production_e2e_readiness(
        tenant=tenant,
        admin_payload=admin,
        marketplace=marketplace,
        whatsapp=whatsapp,
    )
    return {
        "contract_version": "tenant.ops_qa.playbook.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "safe_by_default": True,
        "status": status,
        "score": round((passed / len(checks)) * 100, 2) if checks else 0,
        "summary": {
            "checks_total": len(checks),
            "passed": passed,
            "warnings": sum(1 for item in checks if item.get("status") == "warning"),
            "critical_failed": len(critical_failed),
            "real_messages_sent": 0,
            "e2e_flows_total": (e2e_readiness.get("summary") or {}).get("total"),
            "e2e_flows_ready": (e2e_readiness.get("summary") or {}).get("ready"),
        },
        "checks": checks,
        "e2e_flow_readiness": e2e_readiness,
        "recommended_next_actions": [item for item in checks if not item.get("ok")][:5],
        "execution": {
            "endpoint": f"/api/v2/tenants/{tenant.slug}/ops-qa/check/{{check_id}}",
            "method": "POST",
            "policy": "read_only_safe_checks_only",
            "real_message_policy": "blocked_in_this_playbook",
        },
        "frontend_contract": {
            "render_as": "tenant_ops_qa_command_center",
            "recommended_views": [
                "readiness_score",
                "critical_blockers",
                "safe_check_runner",
                "module_drilldown",
                "e2e_flow_matrix",
            ],
            "refresh_seconds": 30,
            "show_e2e_flow_readiness": True,
        },
    }


def _tenant_ops_qa_execution_result(
    *,
    tenant: TenantProfile,
    check: Mapping[str, Any],
    playbook: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": "tenant.ops_qa.execution.v1",
        "tenant": _tenant_ref(tenant),
        "check_id": check.get("id"),
        "label": check.get("label"),
        "ok": bool(check.get("ok")),
        "status": check.get("status"),
        "severity": check.get("severity"),
        "execution_mode": "read_only",
        "sends_real_message": False,
        "details": check.get("details") or {},
        "next_action": check.get("next_action"),
        "playbook_status": playbook.get("status"),
        "playbook_score": playbook.get("score"),
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
            "supported_types": ["pyme", "municipio", "colegio"],
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
    try:
        limit = max(1, min(int(request.args.get("limit", 20) or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        payload = build_catalog_quality_payload(tenant, limit=limit)
    except Exception:
        current_app.logger.exception(
            "catalog_quality_v2 failed for tenant %s; returning degraded payload",
            getattr(tenant, "slug", None),
        )
        payload = build_catalog_quality_fallback_payload(tenant)
    return _json_response(payload)


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
    try:
        payload = _build_tenant_admin_experience_payload(
            tenant,
            start_date=start_date,
            end_date=end_date,
            app_config=current_app.config,
        )
    except Exception as exc:  # pragma: no cover - defensive production guard
        current_app.logger.exception("[tenant_admin_experience] degraded payload for tenant=%s", getattr(tenant, "slug", None))
        fallback_modules = [
            {
                "id": "inbox",
                "label": "Inbox omnicanal",
                "route": f"/t/{tenant.slug}/inbox",
                "endpoint": "/api/v2/inbox/omnichannel",
                "audience": "admin",
                "secondary_endpoints": [],
                "widgets": ["tickets"],
            },
            {
                "id": "analytics",
                "label": "Metricas y mapas",
                "route": f"/t/{tenant.slug}/analytics",
                "endpoint": "/api/v2/analytics/operations/dashboard",
                "audience": "admin",
                "secondary_endpoints": [],
                "widgets": ["kpis", "heatmap"],
            },
            {
                "id": "employees",
                "label": "Equipo y cobertura",
                "route": f"/t/{tenant.slug}/employees",
                "endpoint": "/api/v2/employee-routing",
                "audience": "admin",
                "secondary_endpoints": [],
                "widgets": ["assignment"],
            },
        ]
        payload = {
            "contract_version": "tenant.admin_experience.v1",
            "tenant": _tenant_ref(tenant),
            "period": {"from": _iso(start_date), "to": _iso(end_date)},
            "modules": fallback_modules,
            "navigation": _admin_navigation_payload(tenant, fallback_modules),
            "health": {
                "contract_version": "tenant.health.v1",
                "status": "degraded",
                "reason_code": "admin_experience_source_failed",
            },
            "frontend_contract": {
                "render_as": "tenant_admin_operating_system",
                "empty_state_behavior": "show_module_readiness_and_next_best_actions",
            },
            "error": {
                "code": 200,
                "message": "admin_experience_degraded",
                "reason_code": "source_failed",
                "detail": str(exc),
            },
        }
    return _json_response(payload)


@v2_saas_bp.route("/tenant/ops-qa/playbook", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/ops-qa/playbook", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_ops_qa_playbook_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    start_date, end_date = _date_range_from_request(default_days=30)
    try:
        payload = _build_tenant_ops_qa_playbook(
            tenant,
            start_date=start_date,
            end_date=end_date,
            app_config=current_app.config,
        )
    except Exception as exc:  # pragma: no cover - defensive degradation for ops UI
        current_app.logger.exception("[tenant_ops_qa] degraded payload for tenant=%s", getattr(tenant, "slug", None))
        payload = {
            "contract_version": "tenant.ops_qa.playbook.v1",
            "tenant": _tenant_ref(tenant),
            "period": {"from": _iso(start_date), "to": _iso(end_date)},
            "safe_by_default": True,
            "status": "fail",
            "score": 0,
            "summary": {"checks_total": 0, "passed": 0, "warnings": 0, "critical_failed": 1, "real_messages_sent": 0},
            "checks": [],
            "recommended_next_actions": [
                {
                    "id": "ops_qa_source_failed",
                    "label": "No se pudo construir el QA operativo",
                    "status": "fail",
                    "severity": "critical",
                    "next_action": "review_backend_logs",
                    "details": {"detail": str(exc)},
                }
            ],
            "frontend_contract": {"render_as": "tenant_ops_qa_command_center"},
        }
    return _json_response(payload)


@v2_saas_bp.route("/tenant/ops-qa/check/<string:check_id>", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/ops-qa/check/<string:check_id>", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_ops_qa_check_v2(current_user, check_id: str, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    normalized_check = str(check_id or "").strip().lower().replace("-", "_")
    start_date, end_date = _date_range_from_request(default_days=30)
    playbook = _build_tenant_ops_qa_playbook(
        tenant,
        start_date=start_date,
        end_date=end_date,
        app_config=current_app.config,
    )
    checks = {
        str(item.get("id")): item
        for item in playbook.get("checks", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    if normalized_check not in checks:
        return _error_response("Check operativo no soportado", 404, "ops_qa_check_not_found", "refresh_playbook")
    return _json_response(
        _tenant_ops_qa_execution_result(
            tenant=tenant,
            check=checks[normalized_check],
            playbook=playbook,
        )
    )


@v2_saas_bp.route("/whatsapp/experience", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/experience", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_experience_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_whatsapp_experience(tenant, app_config=current_app.config))


@v2_saas_bp.route("/integrations/whatsapp/status", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/integrations/whatsapp/status", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_provider_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_whatsapp_provider_status(tenant, current_app.config))


@v2_saas_bp.route("/whatsapp/tech-provider", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error
    return _json_response(build_twilio_tech_provider_contract(tenant, current_app.config))


def _whatsapp_smoke_execution_result(
    *,
    test_id: str,
    ok: bool,
    label: str,
    execution_mode: str,
    danger_level: str,
    details: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": "twilio.tech_provider.smoke_execution.v1",
        "test_id": test_id,
        "ok": bool(ok),
        "status": status or ("pass" if ok else "warning"),
        "label": label,
        "execution_mode": execution_mode,
        "danger_level": danger_level,
        "sends_real_message": danger_level == "real_message",
        "details": dict(details or {}),
        "next_action": next_action or ("continue_playbook" if ok else "review_result"),
    }


@v2_saas_bp.route("/whatsapp/tech-provider/smoke-test/<string:test_id>", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/smoke-test/<string:test_id>", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_smoke_test_v2(current_user, test_id: str, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    normalized_test = str(test_id or "").strip().lower().replace("-", "_")
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    contract = build_twilio_tech_provider_contract(tenant, current_app.config)
    playbook_tests = {
        str(item.get("id")): item
        for item in (contract.get("smoke_playbook") or {}).get("tests", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    if normalized_test not in playbook_tests:
        return _error_response("Smoke test no soportado", 404, "smoke_test_not_found", "refresh_playbook")

    playbook_item = playbook_tests[normalized_test]
    danger_level = str(playbook_item.get("danger_level") or "safe")
    if danger_level == "real_message" and not payload.get("confirm_real_message"):
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=False,
                label=str(playbook_item.get("label") or normalized_test),
                execution_mode=str(playbook_item.get("execution_mode") or "manual_confirmation_required"),
                danger_level=danger_level,
                status="blocked",
                next_action="confirm_real_message_required",
                details={
                    "reason_code": "confirmation_required",
                    "message": "Esta prueba enviaria un mensaje real y requiere confirmacion explicita.",
                },
            ),
            409,
        )

    if normalized_test == "provider_status":
        provider_status = build_whatsapp_provider_status(tenant, current_app.config)
        checks = provider_status.get("readiness_checks") if isinstance(provider_status.get("readiness_checks"), list) else []
        failed = [item for item in checks if isinstance(item, Mapping) and not item.get("ok")]
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=not failed,
                label="Estado del proveedor",
                execution_mode="read_only",
                danger_level="safe",
                details={
                    "checks_total": len(checks),
                    "failed": failed,
                    "next_action": provider_status.get("next_action"),
                    "provider_contract": provider_status.get("contract_version"),
                    "readiness_check_ids": [
                        item.get("id")
                        for item in checks
                        if isinstance(item, Mapping) and item.get("id")
                    ],
                },
                next_action=provider_status.get("next_action") or "continue_playbook",
                status="pass" if not failed else "warning",
            )
        )

    if normalized_test == "whatsapp_experience":
        experience = build_whatsapp_experience(tenant, app_config=current_app.config)
        ok = (
            experience.get("contract_version") == "whatsapp.experience.v1"
            and "conversation_intelligence" in experience
            and "tracking" in experience
        )
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=ok,
                label="Experiencia WhatsApp completa",
                execution_mode="read_only",
                danger_level="safe",
                details={
                    "contract_version": experience.get("contract_version"),
                    "channel": experience.get("channel"),
                    "has_tracking": "tracking" in experience,
                    "has_conversation_intelligence": "conversation_intelligence" in experience,
                },
                next_action="review_operations_hub" if ok else "fix_whatsapp_experience_contract",
            )
        )

    if normalized_test == "template_registry":
        experience = build_whatsapp_experience(tenant, app_config=current_app.config)
        required_templates = (experience.get("templates") or {}).get("required") or []
        vertical_templates = (experience.get("templates") or {}).get("vertical") or {}
        manifest = _template_creation_manifest_payload(
            tenant=tenant,
            required_templates=required_templates if isinstance(required_templates, list) else [],
            vertical_templates=vertical_templates if isinstance(vertical_templates, Mapping) else {},
        )
        blocking = [
            item
            for item in manifest.get("items", [])
            if isinstance(item, Mapping) and (item.get("readiness") or {}).get("severity") == "blocking"
        ]
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=not blocking,
                label="Plantillas y webviews",
                execution_mode="dry_run_first",
                danger_level="safe_when_dry_run",
                details={
                    "manifest_contract": manifest.get("contract_version"),
                    "templates_total": manifest.get("templates_total"),
                    "actionable_total": manifest.get("actionable_total"),
                    "webview_ready_total": manifest.get("webview_ready_total"),
                    "by_content_family": manifest.get("by_content_family"),
                    "blocking": blocking[:5],
                },
                next_action="submit_or_sync_templates" if not blocking else "complete_template_copy_and_samples",
                status="pass" if not blocking else "warning",
            )
        )

    if normalized_test == "sandbox_message":
        sandbox_number = _twilio_sandbox_number()
        join_phrase = _twilio_sandbox_join_phrase(payload)
        wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
        message = str(payload.get("message") or "Hola, quiero probar el asistente").strip()
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=bool(sandbox_number and join_phrase),
                label="Mensaje sandbox",
                execution_mode="copy_or_deeplink",
                danger_level="safe",
                details={
                    "sends_real_message": False,
                    "sandbox_number": f"whatsapp:{sandbox_number}",
                    "join_phrase": join_phrase,
                    "wa_deeplink": f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}",
                    "copy_text": f"{join_phrase}\n\n{message}",
                },
                next_action="open_whatsapp_or_copy_instructions",
            )
        )

    if normalized_test == "production_channel":
        result = poll_whatsapp_sender_status(tenant, current_app.config)
        merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
        sync_twilio_provider_records(
            tenant,
            merged_state,
            app_config=current_app.config,
            actor_user=current_user,
            request_id=_request_id(),
            event_type="twilio_sender_status_smoke",
        )
        flag_modified(tenant, "configuracion")
        db.session.commit()
        sender_status = str(merged_state.get("sender_status") or "").upper()
        ok = sender_status in {"ONLINE", "APPROVED", "CONNECTED", "ACTIVE"}
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=ok,
                label="Canal productivo",
                execution_mode="status_poll",
                danger_level="safe",
                details={
                    "provider_ok": result.get("ok"),
                    "sender_status": merged_state.get("sender_status"),
                    "sender_sid": merged_state.get("sender_sid"),
                    "sender_id": merged_state.get("sender_id"),
                },
                next_action="send_whatsapp_smoke_test" if ok else "wait_for_meta_approval_or_poll_again",
                status="pass" if ok else "warning",
            )
        )

    return _json_response(
        _whatsapp_smoke_execution_result(
            test_id=normalized_test,
            ok=False,
            label=str(playbook_item.get("label") or normalized_test),
            execution_mode=str(playbook_item.get("execution_mode") or "manual"),
            danger_level=danger_level,
            status="blocked",
            next_action="not_implemented_yet",
            details={"reason_code": "execution_not_implemented"},
        ),
        501,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/provision", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/provision", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_provision_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    result = provision_twilio_subaccount(tenant, payload, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_provisioning_plan",
    )
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "twilio_account_sid": merged_state.get("twilio_account_sid"),
                "messaging_service_sid": merged_state.get("messaging_service_sid"),
                "render_subaccount_secret_synced": merged_state.get("render_subaccount_secret_synced"),
                "render_subaccount_secret_sync_status": merged_state.get("render_subaccount_secret_sync_status"),
                "sender_sid": merged_state.get("sender_sid"),
                "sender_id": merged_state.get("sender_id"),
                "updated_at": merged_state.get("updated_at"),
            },
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/voice-app", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/voice-app", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_voice_app_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    result = provision_twilio_voice_application(tenant, payload, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    cfg.update({key: value for key, value in (result.get("tenant_config_patch") or {}).items() if value is not None})
    tenant.configuracion = cfg
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_voice_application",
    )
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "voice_status": merged_state.get("voice_status"),
                "voice_last_step": merged_state.get("voice_last_step"),
                "voice_twiml_app_sid": merged_state.get("voice_twiml_app_sid"),
                "voice_url": merged_state.get("voice_url"),
                "voice_fallback_url": merged_state.get("voice_fallback_url"),
                "voice_status_callback_url": merged_state.get("voice_status_callback_url"),
                "voice_sender_attached": merged_state.get("voice_sender_attached"),
                "updated_at": merged_state.get("updated_at"),
            },
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/embedded-signup", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/embedded-signup", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_embedded_signup_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    auth_response = payload.get("authResponse") if isinstance(payload.get("authResponse"), dict) else {}
    state_patch = {
        "status": "pending_sender_registration",
        "last_step": "embedded_signup_completed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "waba_id": payload.get("waba_id") or payload.get("wabaId"),
        "phone_number_id": payload.get("phone_number_id") or payload.get("phoneNumberId"),
        "embedded_signup_session_id": payload.get("session_id") or payload.get("sessionId"),
        "embedded_signup_code": (
            payload.get("code")
            or payload.get("auth_code")
            or payload.get("authorization_code")
            or auth_response.get("code")
        ),
        "embedded_signup_event": payload.get("event"),
    }
    merged_state = merge_twilio_state(tenant, state_patch)
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="meta_embedded_signup_completed",
    )
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            "contract_version": "twilio.tech_provider.embedded_signup.v1",
            "ok": True,
            "tenant": _tenant_ref(tenant),
            "state": merged_state,
            "next_action": "register_whatsapp_sender_via_senders_api",
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        }
    )


@v2_saas_bp.route("/whatsapp/tech-provider/register-sender", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/register-sender", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_register_sender_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    result = register_whatsapp_sender(tenant, payload, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    voice_result = None
    if result.get("ok", True):
        voice_result = provision_twilio_voice_application(tenant, payload, current_app.config)
        merged_state = merge_twilio_state(tenant, voice_result.get("state_patch") or {})
        cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
        cfg.update({key: value for key, value in (voice_result.get("tenant_config_patch") or {}).items() if value is not None})
        tenant.configuracion = cfg
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_sender_registration",
    )
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "voice_app": voice_result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "twilio_account_sid": merged_state.get("twilio_account_sid"),
                "messaging_service_sid": merged_state.get("messaging_service_sid"),
                "sender_sid": merged_state.get("sender_sid"),
                "sender_id": merged_state.get("sender_id"),
                "sender_status": merged_state.get("sender_status"),
                "waba_id": merged_state.get("waba_id"),
                "phone_number_id": merged_state.get("phone_number_id"),
                "render_subaccount_secret_synced": merged_state.get("render_subaccount_secret_synced"),
                "render_subaccount_secret_sync_status": merged_state.get("render_subaccount_secret_sync_status"),
                "voice_status": merged_state.get("voice_status"),
                "voice_twiml_app_sid": merged_state.get("voice_twiml_app_sid"),
                "voice_sender_attached": merged_state.get("voice_sender_attached"),
                "updated_at": merged_state.get("updated_at"),
            },
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) and (not voice_result or voice_result.get("ok", True)) else 400,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/sender-status", methods=["GET", "POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/sender-status", methods=["GET", "POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_sender_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    result = poll_whatsapp_sender_status(tenant, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_sender_status_poll",
    )
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "sender_sid": merged_state.get("sender_sid"),
                "sender_id": merged_state.get("sender_id"),
                "sender_status": merged_state.get("sender_status"),
                "updated_at": merged_state.get("updated_at"),
            },
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


def _twilio_sandbox_number() -> str:
    raw = (
        current_app.config.get("TWILIO_WHATSAPP_SANDBOX_NUMBER")
        or current_app.config.get("TWILIO_SANDBOX_WHATSAPP_NUMBER")
        or current_app.config.get("TWILIO_WHATSAPP_NUMBER_SANDBOX")
        or "+14155238886"
    )
    value = str(raw or "").strip()
    if value.startswith("whatsapp:"):
        value = value.replace("whatsapp:", "", 1)
    return value or "+14155238886"


def _twilio_sandbox_join_phrase(payload: Mapping[str, Any]) -> str:
    return str(
        payload.get("join_phrase")
        or current_app.config.get("TWILIO_WHATSAPP_SANDBOX_JOIN_PHRASE")
        or current_app.config.get("TWILIO_SANDBOX_JOIN_PHRASE")
        or "join brief-yesterday"
    ).strip()


def _sandbox_quick_menu(tenant: TenantProfile) -> list[dict[str, Any]]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    candidates = [
        cfg.get("quick_menu"),
        (cfg.get("builder_config") or {}).get("quick_menu") if isinstance(cfg.get("builder_config"), dict) else None,
        (cfg.get("widget") or {}).get("quick_menu") if isinstance(cfg.get("widget"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)][:8]
    return []


def _sandbox_demo_context(tenant: TenantProfile, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    sector = str(payload.get("sector") or tenant.vertical or ("gobierno" if tenant.tipo == "municipio" else "empresas")).strip()
    rubro = str(payload.get("rubro") or tenant.subvertical or tenant.vertical or tenant.tipo or "").strip()
    contract = build_demo_whatsapp_sandbox_contract(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro,
        sandbox_number=_twilio_sandbox_number(),
        join_phrase=_twilio_sandbox_join_phrase(payload),
        source=str(payload.get("source") or "tenant_integrations_panel"),
    )
    context = sandbox_context_from_contract(contract)
    context.update(
        {
            "sector": sector,
            "tenant_slug": tenant.slug,
            "rubro": rubro,
            "brief": str(payload.get("brief") or "Probar menu del tenant y crear un caso/pedido/reclamo").strip(),
            "test_message": str(payload.get("test_message") or "Hola, quiero probar el asistente").strip(),
            "quick_menu": payload.get("menu_preview") if isinstance(payload.get("menu_preview"), list) else _sandbox_quick_menu(tenant),
            "widget_config_endpoint": f"/api/public/tenants/{tenant.slug}/widget-config",
            "whatsapp_sandbox": contract,
        }
    )
    return context


@v2_saas_bp.route("/whatsapp/sandbox-session", methods=["OPTIONS"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-session", methods=["OPTIONS"])
def whatsapp_sandbox_session_options_v2(tenant_slug: str | None = None):
    return _json_response({"ok": True, "contract_version": "whatsapp.sandbox_session.v1"})


@v2_saas_bp.route("/whatsapp/sandbox-session", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-session", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_sandbox_session_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    sandbox_number = _twilio_sandbox_number()
    join_phrase = _twilio_sandbox_join_phrase(payload)
    demo_context = _sandbox_demo_context(tenant, payload)
    rubro = demo_context["rubro"]
    brief = demo_context["brief"]
    test_message = demo_context["test_message"]
    wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
    wa_deeplink = f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}"

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    sandbox_sessions = cfg.get("whatsapp_sandbox_sessions")
    if not isinstance(sandbox_sessions, list):
        sandbox_sessions = []
    session_id = f"wsp_sandbox_{uuid.uuid4().hex[:12]}"
    sandbox_sessions.append(
        {
            "id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "whatsapp": payload.get("whatsapp"),
            "join_phrase": join_phrase,
            "rubro": rubro,
            "brief": brief,
            "test_message": test_message,
            "source": payload.get("source") or "tenant_integrations_panel",
        }
    )
    cfg["whatsapp_sandbox_sessions"] = sandbox_sessions[-20:]
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")
    db.session.commit()

    return _json_response(
        {
            "contract_version": "whatsapp.sandbox_session.v1",
            "ok": True,
            "tenant": _tenant_ref(tenant),
            "twilio": {
                "provider": "twilio_sandbox",
                "sandbox_number": f"whatsapp:{sandbox_number}",
                "display_number": "+1 (415) 523-8886" if wa_number == "14155238886" else sandbox_number,
                "join_phrase": join_phrase,
                "wa_deeplink": wa_deeplink,
            },
            "demo_context": {
                "tenant_slug": tenant.slug,
                "rubro": rubro,
                "brief": brief,
                "test_message": test_message,
                "quick_menu": demo_context["quick_menu"],
                "widget_config_endpoint": f"/api/public/tenants/{tenant.slug}/widget-config",
                "trial_policy": demo_context.get("trial_policy"),
                "supported_inputs": demo_context.get("supported_inputs"),
                "catalog": demo_context.get("catalog"),
                "surveys_votings": demo_context.get("surveys_votings"),
            },
            "whatsapp_sandbox": demo_context.get("whatsapp_sandbox"),
            "session": {
                "id": session_id,
                "mode": "copy_or_deeplink",
                "sends_real_message": False,
                "source": payload.get("source") or "tenant_integrations_panel",
            },
        }
    )


@v2_saas_bp.route("/whatsapp/sandbox-setup", methods=["OPTIONS"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-setup", methods=["OPTIONS"])
def whatsapp_sandbox_setup_options_v2(tenant_slug: str | None = None):
    return _json_response({"ok": True, "contract_version": "whatsapp.sandbox_setup.v1"})


@v2_saas_bp.route("/whatsapp/sandbox-setup", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-setup", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_sandbox_setup_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = {
        "sector": request.args.get("sector"),
        "rubro": request.args.get("rubro"),
        "brief": request.args.get("brief"),
        "test_message": request.args.get("test_message"),
    }
    sandbox_number = _twilio_sandbox_number()
    join_phrase = _twilio_sandbox_join_phrase({})
    wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
    enabled = bool(sandbox_number and join_phrase)
    demo_context = _sandbox_demo_context(tenant, payload)

    return _json_response(
        {
            "contract_version": "whatsapp.sandbox_setup.v1",
            "tenant_slug": tenant.slug,
            "tenant": _tenant_ref(tenant),
            "provider": "twilio_whatsapp",
            "enabled": enabled,
            "sandbox": {
                "enabled": enabled,
                "join_number": f"whatsapp:{sandbox_number}",
                "display_number": "+1 (415) 523-8886" if wa_number == "14155238886" else sandbox_number,
                "join_phrase": join_phrase,
                "wa_deeplink": f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}",
                "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={quote_plus(f'https://wa.me/{wa_number}?text={join_phrase}')}",
                "instructions": [
                    {"id": "save_number", "label": "Guarda el numero de prueba"},
                    {"id": "send_phrase", "label": "Envia la frase de activacion"},
                    {"id": "try_menu", "label": "Proba el menu del tenant"},
                ],
            },
            "demo_context": demo_context,
            "whatsapp_sandbox": demo_context.get("whatsapp_sandbox"),
            "test": {
                "endpoint": f"/api/v2/tenants/{tenant.slug}/whatsapp/sandbox-test",
                "method": "POST",
                "payload_template": {"to": "{whatsapp_number}", "message": "{message}"},
            }
            if enabled
            else None,
            "setup_checklist": []
            if enabled
            else [
                {
                    "id": "configure_twilio_sandbox",
                    "label": "Configurar numero y frase de Twilio Sandbox",
                    "required": True,
                }
            ],
            "frontend_contract": {
                "render_as": "whatsapp_sandbox_onboarding",
                "show_preview": True,
                "show_status_check": True,
            },
        }
    )


@v2_saas_bp.route("/whatsapp/sandbox-test", methods=["OPTIONS"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-test", methods=["OPTIONS"])
def whatsapp_sandbox_test_options_v2(tenant_slug: str | None = None):
    return _json_response({"ok": True, "contract_version": "whatsapp.sandbox_test.v1"})


@v2_saas_bp.route("/whatsapp/sandbox-test", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-test", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_sandbox_test_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    sandbox_number = _twilio_sandbox_number()
    join_phrase = _twilio_sandbox_join_phrase(payload)
    wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
    message = str(payload.get("message") or payload.get("test_message") or "Hola, quiero probar el asistente").strip()
    demo_context = _sandbox_demo_context(tenant, payload)

    return _json_response(
        {
            "contract_version": "whatsapp.sandbox_test.v1",
            "ok": True,
            "tenant": _tenant_ref(tenant),
            "mode": "copy_or_deeplink",
            "sends_real_message": False,
            "twilio": {
                "provider": "twilio_sandbox",
                "sandbox_number": f"whatsapp:{sandbox_number}",
                "join_phrase": join_phrase,
                "wa_deeplink": f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}",
            },
            "message_preview": {
                "to": payload.get("to"),
                "message": message,
                "copy_text": f"{join_phrase}\n\n{message}",
            },
            "demo_context": demo_context,
            "whatsapp_sandbox": demo_context.get("whatsapp_sandbox"),
            "next_action": "open_whatsapp_or_copy_instructions",
        }
    )


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


def _smoke_check(
    check_id: str,
    *,
    ok: bool,
    label: str,
    severity: str = "critical",
    details: Mapping[str, Any] | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "status": "pass" if ok else "fail",
        "severity": severity,
        "endpoint": endpoint,
        "details": dict(details or {}),
    }


def _routes_available(paths: list[str]) -> dict[str, bool]:
    registered = {str(rule.rule) for rule in current_app.url_map.iter_rules()}
    return {path: path in registered for path in paths}


def _e2e_flow_qa_guidance(flow_id: str, *, endpoint: str, surface: str) -> dict[str, Any]:
    default_guidance = {
        "frontend_entry": "/perfil",
        "manual_test_steps": [
            "Abrir el modulo operativo del tenant.",
            "Ejecutar el flujo completo con datos de prueba.",
            "Verificar que el evento queda visible para el equipo administrativo.",
        ],
        "acceptance_criteria": [
            "El usuario final recibe una respuesta clara y accionable.",
            "El admin ve el caso con historial, canal y siguiente accion.",
            "El flujo degrada con un estado explicito si falta una integracion.",
        ],
        "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
    }
    guidance_by_flow: dict[str, dict[str, Any]] = {
        "gov_claim_text_to_tracking": {
            "frontend_entry": "/perfil?tab=tickets",
            "manual_test_steps": [
                "Enviar un reclamo por WhatsApp o widget con categoria y direccion.",
                "Confirmar el reclamo y abrir el link publico de seguimiento.",
                "Abrir el CRM y verificar que la conversacion queda en la bandeja de reclamos.",
            ],
            "acceptance_criteria": [
                "Se crea un ticket con codigo, pin y estado inicial.",
                "El link publico muestra resumen, timeline y canal de seguimiento.",
                "El admin puede leer el reclamo, responder y cambiar estado sin salir del CRM.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "claim_live_or_offline_helpdesk": {
            "frontend_entry": "/chat/{ticket_id}?pin={pin}",
            "manual_test_steps": [
                "Abrir el seguimiento publico del reclamo desde el link seguro.",
                "Enviar una consulta desde la mesa de ayuda del ticket.",
                "Verificar en el CRM que el mensaje entra como conversacion del reclamo.",
            ],
            "acceptance_criteria": [
                "El mensaje publico exige pin o token seguro.",
                "Fuera de horario se guarda como offline y mantiene contexto del ticket.",
                "En horario de atencion queda listo para respuesta de un agente humano.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "pyme_catalog_order_checkout": {
            "frontend_entry": "/t/{tenant_slug}/market",
            "manual_test_steps": [
                "Abrir el marketplace publico del tenant.",
                "Buscar productos, agregar items al pedido o subir una nota de pedido.",
                "Confirmar el pedido y verificar que aparece en CRM/pedidos.",
            ],
            "acceptance_criteria": [
                "El catalogo muestra precio, stock, promociones o un estado vacio util.",
                "La nota de pedido se convierte en items sugeridos o en lead de compra.",
                "El checkout o contacto por WhatsApp devuelve confirmacion trazable.",
            ],
            "suggested_command": "python -m pytest tests/test_pedidos_from_file_marketplace.py tests/test_pyme_multimodal_flow.py -q",
        },
        "survey_vote_realtime": {
            "frontend_entry": "/perfil?tab=surveys",
            "manual_test_steps": [
                "Publicar una encuesta o votacion demo.",
                "Votar desde el link publico o QR.",
                "Verificar resultados en vivo, conteos y capas geograficas cuando existan datos.",
            ],
            "acceptance_criteria": [
                "Cada voto queda registrado una sola vez por identidad o sesion permitida.",
                "El panel actualiza resultados sin recargar toda la pagina.",
                "El estado publico diferencia encuesta activa, cerrada y sin datos.",
            ],
            "suggested_command": "python -m pytest tests/product_flow/test_surveys_live_vote_flow.py tests/test_v2_surveys.py -q",
        },
        "school_family_case": {
            "frontend_entry": "/perfil?tab=education",
            "manual_test_steps": [
                "Simular una familia consultando cuota, comprobante o tramite escolar.",
                "Adjuntar comprobante o mensaje desde WhatsApp/widget.",
                "Verificar que el caso queda asignable en el panel del colegio.",
            ],
            "acceptance_criteria": [
                "El agente pide solo los datos faltantes y respeta privacidad.",
                "El comprobante queda asociado al alumno, familia o caso administrativo.",
                "El equipo puede responder y cerrar el caso desde el CRM.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "finance_in_chat_transactional": {
            "frontend_entry": "/perfil?tab=transactions",
            "manual_test_steps": [
                "Iniciar alta, KYC, cobranza, pago o firma desde una conversacion.",
                "Abrir el webview seguro para completar datos sensibles.",
                "Confirmar que el backend recibe webhook server-to-server antes de actualizar estado.",
            ],
            "acceptance_criteria": [
                "Datos sensibles no quedan expuestos en mensajes de WhatsApp.",
                "El webview usa token firmado y estado verificable.",
                "El CRM muestra etapa, comprobante y proxima accion transaccional.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "finance_servicing_transfer_insurance": {
            "frontend_entry": "/perfil?tab=transactions",
            "manual_test_steps": [
                "Probar estado de cuenta, transferencia/remesa, seguro y financiacion.",
                "Verificar que cada journey tenga plantilla, webview y confirmacion backend.",
                "Revisar que el panel muestre trazabilidad por journey.",
            ],
            "acceptance_criteria": [
                "Cada journey financiero declara datos requeridos y riesgos.",
                "Las acciones criticas esperan confirmacion del servicio externo.",
                "El admin ve estado, historial y error recuperable si el proveedor falla.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "analytics_heatmap": {
            "frontend_entry": "/perfil?tab=analytics",
            "manual_test_steps": [
                "Abrir analitica operacional y mapa de calor.",
                "Filtrar por canal, estado, categoria y periodo.",
                "Verificar capas de tickets, pedidos, encuestas y estados sin datos.",
            ],
            "acceptance_criteria": [
                "El mapa no muestra una imagen estatica si no hay datos georreferenciados.",
                "Cada punto o zona de calor abre contexto operativo.",
                "El panel muestra frescura, fuente de datos y fallback cuando falta geocoding.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
    }
    selected = guidance_by_flow.get(flow_id, default_guidance)
    return {
        "frontend_entry": selected["frontend_entry"],
        "manual_test_steps": list(selected["manual_test_steps"]),
        "acceptance_criteria": list(selected["acceptance_criteria"]),
        "automation": {
            "safe_by_default": True,
            "live_side_effects": False,
            "uses_real_whatsapp": False,
            "surface": surface,
            "endpoint": endpoint,
            "suggested_command": selected["suggested_command"],
        },
    }


def _smoke_e2e_flow(
    flow_id: str,
    *,
    label: str,
    ready: bool,
    surface: str,
    endpoint: str,
    evidence: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    qa_scenario_id: str | None = None,
    meta_flow_ready: bool | None = None,
) -> dict[str, Any]:
    status = "ready" if ready else "needs_attention"
    guidance = _e2e_flow_qa_guidance(flow_id, endpoint=endpoint, surface=surface)
    return {
        "id": flow_id,
        "label": label,
        "surface": surface,
        "ready": bool(ready),
        "status": status,
        "endpoint": endpoint,
        "qa_scenario_id": qa_scenario_id,
        "meta_flow_ready": bool(meta_flow_ready) if meta_flow_ready is not None else None,
        "evidence": dict(evidence or {}),
        "next_action": next_action or ("run_live_smoke" if ready else "complete_flow_contract"),
        "frontend_entry": guidance["frontend_entry"],
        "manual_test_steps": guidance["manual_test_steps"],
        "acceptance_criteria": guidance["acceptance_criteria"],
        "automation": guidance["automation"],
    }


def _build_production_e2e_readiness(
    *,
    tenant: TenantProfile | None,
    admin_payload: Mapping[str, Any] | None = None,
    marketplace: Mapping[str, Any] | None = None,
    whatsapp: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    admin_payload = admin_payload or {}
    marketplace = marketplace or {}
    whatsapp = whatsapp or {}
    qa_playbook = whatsapp.get("qa_playbook") if isinstance(whatsapp.get("qa_playbook"), Mapping) else {}
    scenarios = {
        str(item.get("id")): item
        for item in qa_playbook.get("scenarios", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    webviews = whatsapp.get("webview_blueprint") if isinstance(whatsapp.get("webview_blueprint"), Mapping) else {}
    webview_summary = webviews.get("summary") if isinstance(webviews.get("summary"), Mapping) else {}
    flow_states = {
        str(item.get("id")): item
        for item in webviews.get("flows", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    lead_summary = ((admin_payload.get("lead_capture") or {}).get("summary") or {}) if isinstance(admin_payload.get("lead_capture"), Mapping) else {}
    survey_summary = ((admin_payload.get("surveys_votings") or {}).get("summary") or {}) if isinstance(admin_payload.get("surveys_votings"), Mapping) else {}
    freshness = (admin_payload.get("operations") or {}).get("freshness") if isinstance(admin_payload.get("operations"), Mapping) else {}
    freshness_summary = freshness.get("summary") if isinstance(freshness, Mapping) and isinstance(freshness.get("summary"), Mapping) else {}
    market_summary = marketplace.get("summary") if isinstance(marketplace.get("summary"), Mapping) else {}
    commerce = whatsapp.get("commerce") if isinstance(whatsapp.get("commerce"), Mapping) else {}
    checkout = commerce.get("checkout_experience") if isinstance(commerce.get("checkout_experience"), Mapping) else {}
    finance_runtime = whatsapp.get("finance_transactional") if isinstance(whatsapp.get("finance_transactional"), Mapping) else {}
    finance_summary = finance_runtime.get("summary") if isinstance(finance_runtime.get("summary"), Mapping) else {}

    def scenario_ready(scenario_id: str) -> bool:
        scenario = scenarios.get(scenario_id) or {}
        meta_flow = scenario.get("meta_flow_coverage") if isinstance(scenario.get("meta_flow_coverage"), Mapping) else {}
        return bool(scenario and (meta_flow.get("ready") is True or scenario.get("ready") is True))

    def flow_meta_ready(flow_id: str) -> bool:
        flow = flow_states.get(flow_id) or {}
        if "meta_flow_blueprint_ready" in flow:
            return bool(flow.get("meta_flow_blueprint_ready"))
        meta = flow.get("meta_flow_blueprint") if isinstance(flow.get("meta_flow_blueprint"), Mapping) else {}
        return bool(meta.get("screens") and meta.get("data_contract"))

    flows = [
        _smoke_e2e_flow(
            "gov_claim_text_to_tracking",
            label="Municipio: reclamo por WhatsApp hasta seguimiento publico",
            surface="municipios_gobiernos",
            ready=scenario_ready("gov_claim_text_to_tracking") and bool(lead_summary.get("total_recent") is not None),
            endpoint="/api/public/tracking/experience?kind=claim&code={code}&pin={pin}",
            qa_scenario_id="gov_claim_text_to_tracking",
            meta_flow_ready=flow_meta_ready("claim_tracking_helpdesk"),
            evidence={
                "tickets_recent": lead_summary.get("total_recent"),
                "open_tickets": lead_summary.get("open"),
                "webview_flow": "claim_tracking_helpdesk",
                "tracking_contract": (whatsapp.get("tracking") or {}).get("contract_version") if isinstance(whatsapp.get("tracking"), Mapping) else None,
            },
            next_action="run_whatsapp_claim_text_to_tracking_and_open_public_status",
        ),
        _smoke_e2e_flow(
            "claim_live_or_offline_helpdesk",
            label="Mesa de ayuda: chat en vivo u offline del reclamo",
            surface="municipios_gobiernos",
            ready=flow_meta_ready("claim_tracking_helpdesk") and bool(_routes_available(["/api/public/tracking/claims/<int:ticket_id>/messages"]).get("/api/public/tracking/claims/<int:ticket_id>/messages")),
            endpoint="/api/public/tracking/claims/{ticket_id}/messages",
            qa_scenario_id="gov_claim_text_to_tracking",
            meta_flow_ready=flow_meta_ready("claim_tracking_helpdesk"),
            evidence={
                "pin_required": True,
                "public_message_endpoint": "/api/public/tracking/claims/{ticket_id}/messages",
                "working_hours_configurable": True,
            },
            next_action="validate_public_claim_message_reaches_admin_inbox",
        ),
        _smoke_e2e_flow(
            "pyme_catalog_order_checkout",
            label="Pyme: catalogo, carrito, pedido y checkout",
            surface="pymes_empresas",
            ready=scenario_ready("pyme_catalog_order_checkout")
            and (bool(checkout.get("ready")) or int(market_summary.get("products") or 0) > 0),
            endpoint="/api/v2/catalog/quality",
            qa_scenario_id="pyme_catalog_order_checkout",
            meta_flow_ready=flow_meta_ready("catalog_order_builder"),
            evidence={
                "products": market_summary.get("products"),
                "ready_to_sell": market_summary.get("ready_to_sell"),
                "checkout_ready": checkout.get("ready"),
                "webview_flow": "catalog_order_builder",
            },
            next_action="run_catalog_order_checkout_smoke_with_demo_tenant",
        ),
        _smoke_e2e_flow(
            "survey_vote_realtime",
            label="Encuestas y votaciones con resultado en vivo",
            surface="gobiernos_empresas_colegios",
            ready=scenario_ready("survey_vote_realtime")
            and (int(survey_summary.get("active") or 0) > 0 or int(survey_summary.get("responses") or 0) >= 0),
            endpoint="/api/v2/surveys",
            qa_scenario_id="survey_vote_realtime",
            meta_flow_ready=flow_meta_ready("survey_vote"),
            evidence={
                "surveys": survey_summary.get("surveys"),
                "active": survey_summary.get("active"),
                "responses": survey_summary.get("responses"),
                "webview_flow": "survey_vote",
            },
            next_action="publish_demo_survey_and_verify_live_results_heatmap",
        ),
        _smoke_e2e_flow(
            "school_family_case",
            label="Colegios: familia, cuota, comprobante y caso administrativo",
            surface="colegios_educacion",
            ready=scenario_ready("school_family_case") and bool((admin_payload.get("education") or {}).get("profile") is not None),
            endpoint="/api/v2/tenant/admin-experience",
            qa_scenario_id="school_family_case",
            meta_flow_ready=flow_meta_ready("school_payment_receipt") or flow_meta_ready("order_checkout"),
            evidence={
                "education_profile": (admin_payload.get("education") or {}).get("profile") if isinstance(admin_payload.get("education"), Mapping) else None,
                "webview_flow": "school_payment_receipt",
            },
            next_action="run_school_payment_receipt_and_family_case_demo",
        ),
        _smoke_e2e_flow(
            "finance_in_chat_transactional",
            label="Finance in-chat: alta, KYC, cobranza, pago y firma",
            surface="finanzas_pymes_gobiernos_colegios",
            ready=flow_meta_ready("finance_onboarding_kyc")
            and flow_meta_ready("finance_credit_collection_signature")
            and "financial_services" in ((whatsapp.get("template_blueprint") or {}).get("operational_template_groups") or {}),
            endpoint="/api/v2/whatsapp/experience",
            qa_scenario_id="finance_onboarding_collection_signature",
            meta_flow_ready=flow_meta_ready("finance_onboarding_kyc") and flow_meta_ready("finance_credit_collection_signature"),
            evidence={
                "templates_group": "financial_services",
                "onboarding_flow": "finance_onboarding_kyc",
                "operation_flow": "finance_credit_collection_signature",
                "checkout_ready": checkout.get("ready"),
                "finance_runtime": finance_runtime.get("contract_version"),
                "ready_journeys": finance_summary.get("ready_journeys"),
            },
            next_action="run_finance_onboarding_collection_signature_smoke",
        ),
        _smoke_e2e_flow(
            "finance_servicing_transfer_insurance",
            label="Finance avanzado: estado de cuenta, remesas, seguros y financiacion",
            surface="finanzas_banca_seguros",
            ready=bool(finance_runtime.get("contract_version") == "finance.transactional_whatsapp.v1")
            and int(finance_summary.get("journeys") or 0) >= 6
            and flow_meta_ready("finance_account_servicing")
            and flow_meta_ready("finance_remittance_transfer")
            and flow_meta_ready("finance_insurance_claim")
            and flow_meta_ready("finance_fee_financing_tax"),
            endpoint="/api/v2/whatsapp/experience",
            qa_scenario_id="finance_account_servicing",
            meta_flow_ready=flow_meta_ready("finance_account_servicing")
            and flow_meta_ready("finance_remittance_transfer")
            and flow_meta_ready("finance_insurance_claim")
            and flow_meta_ready("finance_fee_financing_tax"),
            evidence={
                "finance_runtime": finance_runtime.get("contract_version"),
                "journeys": finance_summary.get("journeys"),
                "ready_journeys": finance_summary.get("ready_journeys"),
                "webview_flows": [
                    "finance_account_servicing",
                    "finance_remittance_transfer",
                    "finance_insurance_claim",
                    "finance_fee_financing_tax",
                ],
            },
            next_action="run_finance_servicing_transfer_insurance_smoke",
        ),
        _smoke_e2e_flow(
            "analytics_heatmap",
            label="Analitica: mapa de calor, territorios y frescura operacional",
            surface="analytics_maps",
            ready=bool(freshness_summary.get("can_render_heatmap")),
            endpoint="/api/v2/analytics/operations/heatmap",
            evidence={
                "can_render_heatmap": freshness_summary.get("can_render_heatmap"),
                "freshness_status": freshness.get("status") if isinstance(freshness, Mapping) else None,
                "realtime_sources": (whatsapp.get("tracking") or {}).get("realtime_sources") if isinstance(whatsapp.get("tracking"), Mapping) else None,
            },
            next_action="collect_geocoded_claims_orders_and_render_heatmap_layers",
        ),
    ]
    ready_count = sum(1 for item in flows if item.get("ready"))
    return {
        "contract_version": "platform.e2e_flow_readiness.v1",
        "tenant": _tenant_ref(tenant) if tenant else None,
        "status": "ready" if ready_count == len(flows) and flows else "needs_attention",
        "summary": {
            "total": len(flows),
            "ready": ready_count,
            "needs_attention": len(flows) - ready_count,
            "meta_flow_ready": sum(1 for item in flows if item.get("meta_flow_ready")),
            "qa_scenarios": len(scenarios),
            "webview_flows": webview_summary.get("flows_total"),
        },
        "flows": flows,
        "frontend_contract": {
            "render_as": "e2e_flow_readiness_grid",
            "recommended_views": ["flow_cards", "evidence", "next_actions", "qa_scenario_links"],
        },
    }


def _resolve_smoke_tenant(current_user: User, tenant_slug: str | None = None) -> tuple[TenantProfile | None, Any]:
    if is_super_admin_role(getattr(current_user, "rol", None)):
        resolved_slug = tenant_slug or _tenant_slug_from_request()
        if resolved_slug:
            tenant = TenantProfile.query.filter_by(slug=resolved_slug).first()
            if tenant:
                return tenant, None
            return None, _error_response("Tenant no encontrado", 404, "tenant_not_found", "check_tenant_slug")
        tenant = TenantProfile.query.order_by(TenantProfile.created_at.desc()).first()
        if tenant:
            return tenant, None
        return None, None
    return _resolve_tenant_or_error(current_user, tenant_slug)


@v2_saas_bp.route("/production-smoke", methods=["GET"])
@v2_saas_bp.route("/platform/production-smoke", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/production-smoke", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin")
def production_smoke_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_smoke_tenant(current_user, tenant_slug)
    if error:
        return error

    start_date, end_date = _date_range_from_request(default_days=7)
    required_routes = [
        "/api/public/widget-config",
        "/api/v2/demo/session",
        "/ask/pyme",
        "/ask/municipio",
        "/api/ask/pyme",
        "/api/ask/municipio",
        "/api/public/realtime/voice-capabilities",
        "/api/v2/inbox/omnichannel",
        "/api/v2/tenant/admin-experience",
        "/api/v2/whatsapp/experience",
        "/api/v2/catalog/quality",
        "/api/public/tracking/experience",
    ]
    route_status = _routes_available(required_routes)
    e2e_readiness = _build_production_e2e_readiness(tenant=None)
    checks = [
        _smoke_check(
            "routes_registered",
            ok=all(route_status.values()),
            label="Rutas criticas registradas",
            endpoint="flask.url_map",
            details={"routes": route_status},
        )
    ]

    try:
        from routes.public_resolver import _platform_widget_config_payload

        widget_payload = _platform_widget_config_payload()
        onboarding = widget_payload.get("onboarding") or {}
        quick_menu = widget_payload.get("quick_menu") or []
        realtime = widget_payload.get("realtime") or {}
        checks.append(
            _smoke_check(
                "widget_platform_onboarding",
                ok=(
                    widget_payload.get("contract_version") == "public.widget_config.v1"
                    and (widget_payload.get("tenant") or {}).get("slug") == "chatboc-platform"
                    and onboarding.get("mode") == "platform_sector_selector"
                    and len(quick_menu) >= 3
                ),
                label="Widget landing selector plataforma",
                endpoint="/api/public/widget-config",
                details={
                    "tenant": widget_payload.get("tenant"),
                    "onboarding_mode": onboarding.get("mode"),
                    "quick_menu_count": len(quick_menu),
                },
            )
        )
        checks.append(
            _smoke_check(
                "socket_disabled_for_landing",
                ok=not bool(realtime.get("socket_enabled")) or bool(realtime.get("socket_url")),
                label="Socket.IO no se abre sin contrato valido",
                endpoint="/api/public/widget-config",
                details={"realtime": realtime, "visibility_rules": widget_payload.get("visibility_rules")},
            )
        )
    except Exception as exc:
        checks.append(
            _smoke_check(
                "widget_platform_onboarding",
                ok=False,
                label="Widget landing selector plataforma",
                endpoint="/api/public/widget-config",
                details={"error": str(exc)},
            )
        )

    if tenant:
        marketplace = _marketplace_ops_summary(tenant)
        admin_payload = _build_tenant_admin_experience_payload(tenant, start_date=start_date, end_date=end_date, app_config=current_app.config)
        whatsapp = build_whatsapp_experience(tenant, app_config=current_app.config)
        e2e_readiness = _build_production_e2e_readiness(
            tenant=tenant,
            admin_payload=admin_payload,
            marketplace=marketplace,
            whatsapp=whatsapp,
        )
        freshness = (admin_payload.get("operations") or {}).get("freshness") or {}
        first_ticket = TenantTicket.query.filter_by(tenant_id=tenant.id).order_by(TenantTicket.updated_at.desc()).first()
        inbox_item = _inbox_ticket_payload(first_ticket) if first_ticket else None
        checks.extend(
            [
                _smoke_check(
                    "tenant_admin_experience",
                    ok=admin_payload.get("contract_version") == "tenant.admin_experience.v1"
                    and bool(admin_payload.get("modules"))
                    and isinstance(((freshness.get("summary") or {}).get("can_render_heatmap")), bool),
                    label="Tenant Admin OS listo",
                    endpoint="/api/v2/tenant/admin-experience",
                    details={
                        "modules": [item.get("id") for item in admin_payload.get("modules") or []],
                        "can_render_heatmap": (freshness.get("summary") or {}).get("can_render_heatmap"),
                    },
                ),
                _smoke_check(
                    "catalog_quality",
                    ok=marketplace.get("quality", {}).get("contract_version") == "catalog.quality.v1"
                    and "media_capabilities" in marketplace,
                    label="Marketplace quality e imagenes",
                    endpoint="/api/v2/catalog/quality",
                    details={"summary": marketplace.get("summary")},
                ),
                _smoke_check(
                    "whatsapp_operations",
                    ok=whatsapp.get("contract_version") == "whatsapp.experience.v1"
                    and "conversation_intelligence" in whatsapp
                    and "tracking" in whatsapp,
                    label="WhatsApp Operations Hub",
                    endpoint="/api/v2/whatsapp/experience",
                    details={
                        "channel_enabled": (whatsapp.get("channel") or {}).get("enabled"),
                        "voice_enabled": ((whatsapp.get("conversation_intelligence") or {}).get("voice_calls") or {}).get("enabled"),
                    },
                ),
                _smoke_check(
                    "inbox_360",
                    ok=not first_ticket
                    or (
                        bool(inbox_item)
                        and "timeline" in inbox_item
                        and "sla" in inbox_item
                        and "allowed_actions" in inbox_item
                        and "source_metadata" in inbox_item
                    ),
                    label="Inbox 360 drawer contract",
                    endpoint="/api/v2/inbox/omnichannel",
                    details={"has_ticket": bool(first_ticket), "ticket_id": getattr(first_ticket, "id", None)},
                ),
            ]
        )

    failed = [item for item in checks if not item.get("ok")]
    warnings = [item for item in failed if item.get("severity") != "critical"]
    critical = [item for item in failed if item.get("severity") == "critical"]
    status = "pass" if not failed else "warning" if warnings and not critical else "fail"
    payload = {
        "contract_version": "platform.production_smoke.v1",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant) if tenant else None,
        "summary": {
            "total": len(checks),
            "passed": len([item for item in checks if item.get("ok")]),
            "failed": len(failed),
            "critical_failed": len(critical),
            "e2e_flows_total": (e2e_readiness.get("summary") or {}).get("total"),
            "e2e_flows_ready": (e2e_readiness.get("summary") or {}).get("ready"),
        },
        "checks": checks,
        "e2e_flow_readiness": e2e_readiness,
        "frontend_contract": {
            "render_as": "production_smoke_report",
            "recommended_refresh_seconds": 120,
            "fail_http_query_param": "fail_http=1",
            "show_e2e_flow_readiness": True,
            "show_flow_evidence": True,
        },
    }
    http_status = 500 if status == "fail" and request.args.get("fail_http") == "1" else 200
    return _json_response(payload, http_status)


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
    items = [_inbox_ticket_payload(ticket) for ticket in tickets]

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
            "frontend_contract": {
                "render_as": "omnichannel_inbox",
                "detail_endpoint_template": "/api/v2/inbox/omnichannel/{ticket_id}",
                "drawer_contract": "inbox.omnichannel.detail.v1",
            },
        }
    )


def _attachment_items(extra: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = (
        extra.get("attachments")
        or extra.get("attachmentInfo")
        or extra.get("attachment_info")
        or extra.get("uploaded_file_info")
        or extra.get("files")
        or []
    )
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("file_url") or item.get("public_url")
        mime_type = item.get("mimeType") or item.get("mime_type") or item.get("content_type")
        items.append(
            {
                "id": item.get("id") or item.get("key") or f"attachment-{index + 1}",
                "name": item.get("name") or item.get("filename") or item.get("file_name") or f"Adjunto {index + 1}",
                "url": url,
                "mimeType": mime_type,
                "size": item.get("size") or item.get("bytes"),
                "kind": item.get("kind") or ("image" if str(mime_type or "").startswith("image/") else "file"),
                "source": item.get("source") or "chat_attachment",
            }
        )
    return items


def _timeline_items(extra: Mapping[str, Any], *, limit: int = 30) -> list[dict[str, Any]]:
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    timeline = []
    for comment in comments[-limit:]:
        if not isinstance(comment, dict):
            continue
        origin = comment.get("origin") or ("admin_panel" if comment.get("visibility") == "internal" else "public_tracking")
        timeline.append(
            {
                "id": comment.get("id"),
                "type": comment.get("type") or "message",
                "origin": origin,
                "body": comment.get("body") or "",
                "visibility": comment.get("visibility") or "public",
                "created_at": comment.get("created_at"),
                "actor": comment.get("actor") if isinstance(comment.get("actor"), dict) else None,
                "action": comment.get("action"),
                "attachments": _attachment_items(comment),
            }
        )
    return timeline


def _ticket_sla_payload(ticket: TenantTicket, extra: Mapping[str, Any]) -> dict[str, Any]:
    sla = extra.get("sla") if isinstance(extra.get("sla"), dict) else {}
    overdue = is_ticket_overdue(ticket)
    status = "breached" if overdue else str(extra.get("sla_status") or extra.get("sla_state") or "ok")
    return {
        "status": status,
        "overdue": overdue,
        "priority": extra.get("priority") or "medium",
        "first_response_due_at": sla.get("first_response_due_at"),
        "resolution_due_at": sla.get("resolution_due_at"),
        "next_update_due_at": sla.get("next_update_due_at"),
        "paused": bool(sla.get("paused")),
    }


def _allowed_inbox_actions(ticket: TenantTicket, extra: Mapping[str, Any]) -> list[dict[str, Any]]:
    status = str(ticket.estado or "").lower()
    base_endpoint = f"/api/v2/inbox/omnichannel/{ticket.id}/actions"
    actions = [
        {"id": "reply", "label": "Responder", "method": "POST", "endpoint": base_endpoint, "requires": ["body"]},
        {"id": "assign", "label": "Asignar", "method": "POST", "endpoint": base_endpoint, "requires": ["assignee_id"]},
        {"id": "handoff", "label": "Derivar", "method": "POST", "endpoint": base_endpoint, "requires": ["channel"]},
        {"id": "set_priority", "label": "Cambiar prioridad", "method": "POST", "endpoint": base_endpoint, "requires": ["priority"]},
    ]
    if status in _CLOSED_TICKET_STATES:
        actions.append({"id": "reopen", "label": "Reabrir", "method": "POST", "endpoint": base_endpoint, "requires": []})
    else:
        actions.append({"id": "close", "label": "Cerrar", "method": "POST", "endpoint": base_endpoint, "requires": [], "destructive": True})
    return actions


def _next_steps(ticket: TenantTicket, extra: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = []
    if not extra.get("assignee_id"):
        steps.append({"id": "assign_owner", "label": "Asignar responsable", "action": "assign", "priority": "high"})
    if str(ticket.estado or "").lower() not in _CLOSED_TICKET_STATES:
        steps.append({"id": "reply_customer", "label": "Responder al contacto", "action": "reply", "priority": "medium"})
    if ticket.latitud is None and ticket.longitud is None and not extra.get("address"):
        steps.append({"id": "collect_location", "label": "Pedir ubicacion si aplica", "action": "reply", "priority": "low"})
    if _ticket_channel(ticket) == "whatsapp":
        steps.append({"id": "whatsapp_followup", "label": "Continuar por WhatsApp", "action": "reply", "priority": "medium"})
    return steps[:4]


def _source_metadata(ticket: TenantTicket, extra: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "origin": ticket.origen,
        "channel": _ticket_channel(ticket),
        "conversation_id": extra.get("conversation_id") or f"ticket-{ticket.id}",
        "chat_session_id": extra.get("chat_session_id") or extra.get("session_id"),
        "demo_session_id": extra.get("demo_session_id"),
        "widget_id": extra.get("widget_id"),
        "contact_key": extra.get("contact_key"),
        "whatsapp_message_id": extra.get("whatsapp_message_id"),
        "lead_source": extra.get("lead_source") or ticket.origen,
        "demo_mode": bool(extra.get("demo_mode")),
    }


def _inbox_ticket_payload(ticket: TenantTicket) -> dict[str, Any]:
    extra = _ticket_extra(ticket)
    timeline = _timeline_items(extra)

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
        "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}",
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "description": ticket.descripcion,
        "preview_text": extra.get("preview_text") or ticket.descripcion,
        "status": ticket.estado,
        "priority": extra.get("priority") or "medium",
        "channel": _ticket_channel(ticket),
        "category": ticket.categoria,
        "intent": extra.get("intent") or extra.get("lead_intent") or ticket.categoria,
        "assignee": assignee,
        "contact": extra.get("contact") if isinstance(extra.get("contact"), dict) else {},
        "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": extra.get("address")},
        "map": {
            "can_render": ticket.latitud is not None and ticket.longitud is not None,
            "fallback_when_no_coordinates": "timeline_only",
        },
        "attachments": _attachment_items(extra),
        "sla": _ticket_sla_payload(ticket, extra),
        "timeline": timeline,
        "presence": extra.get("presence") if isinstance(extra.get("presence"), dict) else {"viewers": [], "locked_by": None},
        "actions": [item["id"] for item in _allowed_inbox_actions(ticket, extra)],
        "allowed_actions": _allowed_inbox_actions(ticket, extra),
        "next_steps": _next_steps(ticket, extra),
        "source_metadata": _source_metadata(ticket, extra),
        "handoff": extra.get("handoff") if isinstance(extra.get("handoff"), dict) else None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "frontend_contract": {
            "render_as": "inbox_360_drawer",
            "timeline_component": "conversation_timeline",
            "map_fallback": "timeline_only",
        },
    }


@v2_saas_bp.route("/inbox/omnichannel/<int:ticket_id>", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def omnichannel_inbox_detail_v2(current_user, ticket_id: int):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    ticket = TenantTicket.query.filter_by(id=ticket_id, tenant_id=tenant.id).first()
    if not ticket:
        return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")
    item = _inbox_ticket_payload(ticket)
    return _json_response(
        {
            "contract_version": "inbox.omnichannel.detail.v1",
            "tenant": _tenant_ref(tenant),
            "item": item,
            "ticket": item,
        }
    )


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
