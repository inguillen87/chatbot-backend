from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func

from extensions import db
from models import Notification, NotificationTemplate, TenantProfile, TenantTicket, User
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.v2.sla_service import is_ticket_overdue
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
        "plan": tenant.plan,
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


@v2_saas_bp.route("/tenant-health", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/health", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_health_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(_tenant_health_payload(tenant))


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
