"""Backoffice contracts for the authenticated tenant control center."""

from __future__ import annotations

import csv
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_

from extensions import db
from models import (
    EncComentario,
    EncEncuesta,
    EncRespuesta,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TicketComentario,
    TicketRealtimeState,
    User,
)
from services.plan_access import integration_access_payload
from utils.auth_helpers import token_requerido
from utils.roles import canonical_role, first_specific_tenant_slug, is_super_admin_role, normalize_tenant_slug

backoffice_bp = Blueprint("backoffice", __name__, url_prefix="/api/app/backoffice")
backoffice_v2_bp = Blueprint("backoffice_v2", __name__, url_prefix="/api/v2/backoffice")

_CLOSED_STATES = {"resuelto", "resuelta", "cerrado", "cerrada", "finalizado", "finalizada", "completado", "completada"}
_PENDING_STATES = {"nuevo", "nueva", "pendiente", "en_progreso", "abierto", "abierta", "asignado", "asignada"}


def _request_id() -> str:
    inbound = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return inbound or f"req_{uuid.uuid4().hex}"


def _json(payload: dict[str, Any], *, status: int = 200, request_id: str | None = None):
    resolved_request_id = request_id or payload.get("request_id") or _request_id()
    payload.setdefault("request_id", resolved_request_id)
    response = jsonify(payload)
    response.status_code = status
    response.headers.setdefault("X-Request-Id", resolved_request_id)
    return response


def _normalize_slug(value: Any) -> str:
    return normalize_tenant_slug(value)


def _resolve_tenant(current_user: User) -> TenantProfile | None:
    slug = first_specific_tenant_slug(
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
        getattr(current_user, "tenant_slug", None),
    )
    if slug:
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug).first()
        if tenant:
            return tenant

    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id:
        tenant = db.session.get(TenantProfile, int(tenant_id))
        if tenant:
            return tenant

    return (
        TenantProfile.query.filter(
            or_(
                TenantProfile.municipio_id == current_user.id,
                TenantProfile.pyme_id == current_user.id,
            )
        )
        .order_by(TenantProfile.id.asc())
        .first()
    )


def _is_authorized(current_user: User, tenant: TenantProfile) -> bool:
    role = canonical_role(getattr(current_user, "rol", None))
    if is_super_admin_role(role):
        return True
    if getattr(current_user, "tenant_id", None) == tenant.id:
        return True
    if _normalize_slug(getattr(current_user, "tenant_slug", None)) == _normalize_slug(tenant.slug):
        return True
    if tenant.municipio_id and tenant.municipio_id == current_user.id:
        return True
    if tenant.pyme_id and tenant.pyme_id == current_user.id:
        return True
    owner_id = tenant.municipio_id or tenant.pyme_id
    if owner_id and getattr(current_user, "empresa_id", None) == owner_id:
        return True
    return False


def _tenant_scope(tenant: TenantProfile) -> str:
    raw = str(tenant.tipo or tenant.vertical or "").strip().lower()
    if raw in {"municipio", "gobierno", "government"}:
        return "municipio"
    if raw in {"colegio", "educacion", "educación", "school"}:
        return "colegio"
    if raw in {"pyme", "empresa", "commerce", "comercio"}:
        return "pyme"
    return raw or "tenant"


def _capabilities(tenant: TenantProfile) -> dict[str, Any]:
    raw = tenant.capabilities_json if isinstance(tenant.capabilities_json, dict) else {}
    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    merged = dict(config.get("capabilities") or {})
    merged.update(raw)
    return merged


def _capability_enabled(capabilities: dict[str, Any], key: str, *, default: bool) -> bool:
    value = capabilities.get(key)
    if isinstance(value, dict):
        if "enabled" in value:
            return bool(value.get("enabled"))
        return default
    if value is None and isinstance(capabilities.get("backoffice"), dict):
        value = capabilities["backoffice"].get(key)
    if value is None and isinstance(capabilities.get("analytics"), dict):
        value = capabilities["analytics"].get(key)
    if value is None:
        return default
    return bool(value)


def _integration_access(tenant: TenantProfile) -> dict[str, Any]:
    return integration_access_payload(tenant)


def _feature_access(access: dict[str, Any], feature_id: str) -> dict[str, Any]:
    features = access.get("features") if isinstance(access.get("features"), dict) else {}
    feature = features.get(feature_id)
    if isinstance(feature, dict):
        return feature
    return {
        "id": feature_id,
        "enabled": bool(access.get("enabled")),
        "status": access.get("status") or ("enabled" if access.get("enabled") else "locked"),
        "reason_code": access.get("reason_code"),
        "lock_reason_code": access.get("lock_reason_code"),
        "required_plan": access.get("required_plan") or "full",
    }


def _feature_enabled(access: dict[str, Any], feature_id: str) -> bool:
    return bool(_feature_access(access, feature_id).get("enabled"))


def _integration_plan_required_response(tenant: TenantProfile, request_id: str, feature_id: str):
    access = _integration_access(tenant)
    feature = _feature_access(access, feature_id)
    return _json(
        {
            "ok": False,
            "contract_version": access.get("contract_version") or "tenant.integration_access.v1",
            "reason_code": feature.get("reason_code") or access.get("reason_code") or "plan_full_required",
            "lock_reason_code": feature.get("lock_reason_code") or access.get("lock_reason_code"),
            "message": access.get("message"),
            "feature_id": feature_id,
            "feature": feature,
            "access": access,
            "frontend_contract": {
                **(access.get("frontend_contract") or {}),
                "render_as": "integration_locked_state",
                "feature_id": feature_id,
            },
        },
        status=403,
        request_id=request_id,
    )


def _analytics_modes(tenant: TenantProfile, current_user: User) -> dict[str, Any]:
    capabilities = _capabilities(tenant)
    access = _integration_access(tenant)
    advanced_allowed = _feature_enabled(access, "analytics_dashboard")
    advanced_enabled = _capability_enabled(
        capabilities,
        "advanced_analytics",
        default=advanced_allowed,
    ) and advanced_allowed
    return {
        "statistics": {
            "label": "Estadisticas",
            "description": "Tablero operativo simple para administracion diaria.",
            "enabled": _capability_enabled(capabilities, "statistics", default=True),
        },
        "advanced_analytics": {
            "label": "Analitica IA",
            "description": "Investigacion, segmentos, resumen ejecutivo y exportaciones.",
            "enabled": advanced_enabled,
            "access": _feature_access(access, "analytics_dashboard"),
        },
    }


def _parse_window(value: str | None) -> tuple[datetime, str]:
    raw = str(value or "7d").strip().lower()
    amount_text = raw[:-1] if raw and raw[-1] in {"d", "h"} else raw
    try:
        amount = int(amount_text)
    except (TypeError, ValueError):
        amount = 7
        raw = "7d"
    amount = max(1, min(amount, 90))
    now = datetime.now(timezone.utc)
    if raw.endswith("h"):
        return now - timedelta(hours=amount), f"{amount}h"
    return now - timedelta(days=amount), f"{amount}d"


def _tenant_id_candidates(tenant: TenantProfile) -> list[int]:
    candidates = [tenant.id]
    if tenant.encuestas_tenant_id:
        candidates.append(int(tenant.encuestas_tenant_id))
    return list(dict.fromkeys(candidates))


def _surveys_overview(tenant: TenantProfile, *, since: datetime | None = None) -> dict[str, Any]:
    tenant_ids = _tenant_id_candidates(tenant)
    surveys_query = EncEncuesta.query.filter(EncEncuesta.tenant_id.in_(tenant_ids))
    active_surveys = surveys_query.filter(EncEncuesta.estado == "publicada").count()
    survey_ids = [row.id for row in surveys_query.with_entities(EncEncuesta.id).all()]

    responses_query = EncRespuesta.query.filter(EncRespuesta.tenant_id.in_(tenant_ids))
    if since is not None:
        responses_query = responses_query.filter(EncRespuesta.submitted_at >= since)

    comments_pending_review = 0
    if survey_ids:
        comments_pending_review = (
            EncComentario.query.filter(EncComentario.encuesta_id.in_(survey_ids))
            .filter(or_(EncComentario.estado == "revision", EncComentario.report_count > 0))
            .count()
        )

    heatmap_available = responses_query.filter(
        EncRespuesta.lat.isnot(None),
        EncRespuesta.lng.isnot(None),
    ).count() > 0

    return {
        "active_surveys": int(active_surveys),
        "live_votes": int(responses_query.count()),
        "comments_pending_review": int(comments_pending_review),
        "heatmap_available": bool(heatmap_available),
        "route": "/admin/encuestas",
    }


def _case_queries(tenant: TenantProfile, *, since: datetime):
    scope = _tenant_scope(tenant)
    if scope == "municipio":
        query = MunicipioTicket.query.filter(
            or_(
                MunicipioTicket.tenant_id == tenant.id,
                MunicipioTicket.municipio_id == tenant.municipio_id,
            )
        )
        return [query.filter(MunicipioTicket.fecha >= since)]
    if scope == "pyme":
        tickets = PymeTicket.query.filter(PymeTicket.tenant_id == tenant.id).filter(PymeTicket.fecha >= since)
        pedidos = PymePedido.query.filter(
            or_(
                PymePedido.tenant_id == tenant.id,
                PymePedido.pyme_id == tenant.pyme_id,
            )
        ).filter(PymePedido.fecha >= since)
        return [tickets, pedidos]
    tickets = MunicipioTicket.query.filter(MunicipioTicket.tenant_id == tenant.id).filter(MunicipioTicket.fecha >= since)
    return [tickets]


def _count_states(query, model, states: set[str]) -> int:
    lowered_states = [state.lower() for state in states]
    return int(query.filter(func.lower(model.estado).in_(lowered_states)).count())


def _operations_counts(tenant: TenantProfile, *, since: datetime) -> dict[str, int]:
    scope = _tenant_scope(tenant)
    pending = 0
    resolved = 0
    total = 0
    geo_points = 0
    if scope == "pyme":
        ticket_query, pedidos_query = _case_queries(tenant, since=since)
        pending += _count_states(ticket_query, PymeTicket, _PENDING_STATES)
        resolved += _count_states(ticket_query, PymeTicket, _CLOSED_STATES)
        total += int(ticket_query.count())
        geo_points += int(ticket_query.filter(PymeTicket.latitud.isnot(None), PymeTicket.longitud.isnot(None)).count())
        pending += _count_states(pedidos_query, PymePedido, {"pendiente", "nuevo", "en_progreso"})
        resolved += _count_states(pedidos_query, PymePedido, {"finalizado", "completado", "entregado", "pagado"})
        total += int(pedidos_query.count())
        geo_points += int(pedidos_query.filter(PymePedido.latitud.isnot(None), PymePedido.longitud.isnot(None)).count())
    else:
        query = _case_queries(tenant, since=since)[0]
        pending += _count_states(query, MunicipioTicket, _PENDING_STATES)
        resolved += _count_states(query, MunicipioTicket, _CLOSED_STATES)
        total += int(query.count())
        geo_points += int(query.filter(MunicipioTicket.latitud.isnot(None), MunicipioTicket.longitud.isnot(None)).count())
    return {"pending": pending, "resolved": resolved, "total": total, "geo_points": geo_points}


def _top_pending_category(tenant: TenantProfile, *, since: datetime) -> tuple[str | None, int]:
    scope = _tenant_scope(tenant)
    if scope == "pyme":
        row = (
            PymeTicket.query.filter(PymeTicket.tenant_id == tenant.id, PymeTicket.fecha >= since)
            .filter(func.lower(PymeTicket.estado).in_([state.lower() for state in _PENDING_STATES]))
            .with_entities(PymeTicket.categoria, func.count(PymeTicket.id).label("total"))
            .group_by(PymeTicket.categoria)
            .order_by(func.count(PymeTicket.id).desc())
            .first()
        )
    else:
        row = (
            MunicipioTicket.query.filter(
                or_(
                    MunicipioTicket.tenant_id == tenant.id,
                    MunicipioTicket.municipio_id == tenant.municipio_id,
                ),
                MunicipioTicket.fecha >= since,
            )
            .filter(func.lower(MunicipioTicket.estado).in_([state.lower() for state in _PENDING_STATES]))
            .with_entities(MunicipioTicket.categoria, func.count(MunicipioTicket.id).label("total"))
            .group_by(MunicipioTicket.categoria)
            .order_by(func.count(MunicipioTicket.id).desc())
            .first()
        )
    if not row:
        return None, 0
    return str(row[0] or "Sin categoria"), int(row[1] or 0)


def _modules_for(tenant: TenantProfile, current_user: User, *, analytics_modes: dict[str, Any], surveys: dict[str, Any], counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
    role = canonical_role(getattr(current_user, "rol", None)) or "usuario"
    scope = _tenant_scope(tenant)
    capabilities = _capabilities(tenant)
    counts = counts or {"geo_points": 0}
    access = _integration_access(tenant)
    people_enabled = role in {"admin", "super_admin", "empleado"} and _capability_enabled(capabilities, "people", default=True)
    surveys_access = _feature_access(access, "surveys_votings")
    surveys_enabled = bool(surveys_access.get("enabled")) and _capability_enabled(capabilities, "surveys", default=True)
    maps_default = scope in {"municipio", "colegio"} or bool(counts.get("geo_points"))
    maps_access = _feature_access(access, "heatmaps")
    maps_enabled = bool(maps_access.get("enabled")) and _capability_enabled(capabilities, "maps", default=maps_default)
    analytics_access = _feature_access(access, "analytics_dashboard")
    comments_access = _feature_access(access, "comments_inbox")

    modules = [
        {
            "id": "operations",
            "label": "Operar reclamos" if scope == "municipio" else "Operar casos y pedidos",
            "description": "Reclamos, estados, ubicaciones y seguimiento diario." if scope == "municipio" else "Casos, pedidos, estados y seguimiento diario.",
            "route": "/perfil?tab=tickets",
            "enabled": _capability_enabled(capabilities, "operations", default=True),
            "access": comments_access,
            "priority": 1,
        },
        {
            "id": "reports",
            "label": "Reportes claros",
            "description": "Resumen operativo, mapas de calor y prioridades.",
            "route": "/perfil?tab=estadisticas",
            "enabled": bool((analytics_modes.get("statistics") or {}).get("enabled")),
            "access": analytics_access,
            "priority": 2,
        },
        {
            "id": "surveys",
            "label": "Encuestas y sondeos",
            "description": "Participacion, votaciones, comentarios y resultados en vivo.",
            "route": "/admin/encuestas",
            "enabled": surveys_enabled,
            "access": surveys_access,
            "priority": 3,
        },
        {
            "id": "people",
            "label": "Personas y accesos",
            "description": "Empleados, roles, permisos y asignacion por categoria.",
            "route": "/empleados",
            "enabled": people_enabled,
            "priority": 4,
        },
        {
            "id": "maps",
            "label": "Mapas de calor",
            "description": "Zonas, categorias y demanda georreferenciada.",
            "route": "/perfil?tab=estadisticas&view=mapas",
            "enabled": maps_enabled,
            "access": maps_access,
            "priority": 5,
        },
        {
            "id": "advanced_analytics",
            "label": "Analitica IA",
            "description": "Segmentos, resumen ejecutivo, investigacion y exportaciones.",
            "route": "/analytics?mode=advanced",
            "enabled": bool((analytics_modes.get("advanced_analytics") or {}).get("enabled")),
            "access": analytics_access,
            "priority": 6,
        },
    ]
    return sorted(modules, key=lambda item: int(item.get("priority") or 999))


def _backoffice_actions(tenant: TenantProfile, *, analytics_modes: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities = _capabilities(tenant)
    access = _integration_access(tenant)
    analytics_access = _feature_access(access, "analytics_dashboard")
    statistics_enabled = bool((analytics_modes.get("statistics") or {}).get("enabled"))
    advanced_enabled = bool((analytics_modes.get("advanced_analytics") or {}).get("enabled"))
    exports_enabled = bool(analytics_access.get("enabled")) and _capability_enabled(capabilities, "exports", default=statistics_enabled)
    actions: list[dict[str, Any]] = []
    actions.append(
        {
            "id": "export_backoffice",
            "label": "Exportar datos",
            "description": "Generar PDF, CSV o XLSX desde filtros reales.",
            "method": "POST",
            "endpoint": "/api/v2/backoffice/export",
            "formats": ["pdf", "csv", "xlsx"],
            "enabled": exports_enabled,
            "access": analytics_access,
        }
    )
    actions.append(
        {
            "id": "executive_summary",
            "label": "Pedir resumen IA",
            "description": "Resumen ejecutivo con riesgos, oportunidades y calidad de datos.",
            "method": "POST",
            "endpoint": "/api/v2/backoffice/executive-summary",
            "enabled": advanced_enabled,
            "access": analytics_access,
        }
    )
    return actions


def _navigation_payload(current_user: User, tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    analytics_modes = _analytics_modes(tenant, current_user)
    surveys = _surveys_overview(tenant)
    counts = _operations_counts(tenant, since=datetime.now(timezone.utc) - timedelta(days=7))
    access = _integration_access(tenant)
    return {
        "contract_version": "backoffice.navigation.v1",
        "tenant_slug": tenant.slug,
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.nombre,
            "scope": _tenant_scope(tenant),
            "plan": tenant.plan,
        },
        "role": canonical_role(getattr(current_user, "rol", None)) or "usuario",
        "modules": _modules_for(tenant, current_user, analytics_modes=analytics_modes, surveys=surveys, counts=counts),
        "analytics_modes": analytics_modes,
        "surveys_overview": surveys,
        "actions": _backoffice_actions(tenant, analytics_modes=analytics_modes),
        "access": access,
        "request_id": request_id,
    }


def _summary_payload(current_user: User, tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    since, normalized_window = _parse_window(request.args.get("window"))
    counts = _operations_counts(tenant, since=since)
    surveys = _surveys_overview(tenant, since=since)
    analytics_modes = _analytics_modes(tenant, current_user)
    access = _integration_access(tenant)
    top_category, top_category_count = _top_pending_category(tenant, since=since)

    cards = [
        {"id": "pending_cases", "label": "Pendientes", "value": counts["pending"], "tone": "warning" if counts["pending"] else "neutral"},
        {"id": "resolved_cases", "label": "Resueltos", "value": counts["resolved"], "tone": "success" if counts["resolved"] else "neutral"},
        {"id": "active_surveys", "label": "Encuestas activas", "value": surveys["active_surveys"], "tone": "info" if surveys["active_surveys"] else "neutral"},
        {"id": "live_votes", "label": "Votos y respuestas", "value": surveys["live_votes"], "tone": "info" if surveys["live_votes"] else "neutral"},
    ]

    priorities: list[dict[str, Any]] = []
    if counts["pending"]:
        priorities.append(
            {
                "id": "pending_cases",
                "label": "Hay casos pendientes por resolver",
                "description": f"{counts['pending']} casos siguen abiertos en la ventana seleccionada.",
                "action": {"label": "Ver operacion", "route": "/perfil?tab=tickets"},
            }
        )
    if top_category and top_category_count:
        priorities.append(
            {
                "id": "top_pending_category",
                "label": f"{top_category} requiere atencion",
                "description": f"{top_category_count} casos pendientes comparten esta categoria.",
                "action": {"label": "Ver mapa", "route": "/perfil?tab=estadisticas"},
            }
        )
    if surveys["comments_pending_review"]:
        priorities.append(
            {
                "id": "survey_comments_review",
                "label": "Hay comentarios de encuestas para revisar",
                "description": f"{surveys['comments_pending_review']} comentarios tienen revision o reportes.",
                "action": {"label": "Abrir encuestas", "route": "/admin/encuestas"},
            }
        )

    return {
        "contract_version": "backoffice.summary.v1",
        "tenant_slug": tenant.slug,
        "window": normalized_window,
        "title": f"Resumen de los ultimos {normalized_window}",
        "cards": cards,
        "priorities": priorities,
        "ai_summary_available": bool((analytics_modes.get("advanced_analytics") or {}).get("enabled")),
        "analytics_modes": analytics_modes,
        "surveys_overview": surveys,
        "modules": _modules_for(tenant, current_user, analytics_modes=analytics_modes, surveys=surveys, counts=counts),
        "actions": _backoffice_actions(tenant, analytics_modes=analytics_modes),
        "access": access,
        "request_id": request_id,
    }


@backoffice_bp.get("/navigation")
@token_requerido
def backoffice_navigation(current_user: User):
    request_id = _request_id()
    tenant = _resolve_tenant(current_user)
    if not tenant:
        return _json(
            {
                "ok": False,
                "reason_code": "tenant_not_found",
                "message": "No se encontro el tenant solicitado.",
            },
            status=404,
            request_id=request_id,
        )
    if not _is_authorized(current_user, tenant):
        return _json(
            {
                "ok": False,
                "reason_code": "tenant_forbidden",
                "message": "No tenes permisos para este tenant.",
            },
            status=403,
            request_id=request_id,
        )
    return _json(_navigation_payload(current_user, tenant, request_id), request_id=request_id)


@backoffice_bp.get("/summary")
@token_requerido
def backoffice_summary(current_user: User):
    request_id = _request_id()
    tenant = _resolve_tenant(current_user)
    if not tenant:
        return _json(
            {
                "ok": False,
                "reason_code": "tenant_not_found",
                "message": "No se encontro el tenant solicitado.",
            },
            status=404,
            request_id=request_id,
        )
    if not _is_authorized(current_user, tenant):
        return _json(
            {
                "ok": False,
                "reason_code": "tenant_forbidden",
                "message": "No tenes permisos para este tenant.",
            },
            status=403,
            request_id=request_id,
        )
    return _json(_summary_payload(current_user, tenant, request_id), request_id=request_id)


# ---------------------------------------------------------------------------
# /api/v2/backoffice operations contracts

_ORDER_FINAL_STATES = {
    "finalizado",
    "finalizada",
    "completado",
    "completada",
    "entregado",
    "entregada",
    "cancelado",
    "cancelada",
}
_ORDER_CONFIRMED_REVENUE_STATES = {
    "pagado",
    "pagada",
    "finalizado",
    "finalizada",
    "completado",
    "completada",
    "entregado",
    "entregada",
}


def _resolve_tenant_for_v2(current_user: User, explicit_slug: str | None = None) -> TenantProfile | None:
    slug = _normalize_slug(explicit_slug)
    if slug:
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug).first()
        if tenant:
            return tenant
    return _resolve_tenant(current_user)


def _authorized_tenant_or_response(
    current_user: User,
    request_id: str,
    *,
    explicit_slug: str | None = None,
) -> tuple[TenantProfile | None, Any | None]:
    tenant = _resolve_tenant_for_v2(current_user, explicit_slug=explicit_slug)
    if not tenant:
        return None, _json(
            {
                "ok": False,
                "reason_code": "tenant_not_found",
                "message": "No se encontro el tenant solicitado.",
            },
            status=404,
            request_id=request_id,
        )
    if not _is_authorized(current_user, tenant):
        return None, _json(
            {
                "ok": False,
                "reason_code": "tenant_forbidden",
                "message": "No tenes permisos para este tenant.",
            },
            status=403,
            request_id=request_id,
        )
    return tenant, None


def _requested_scope(tenant: TenantProfile) -> str:
    requested = _normalize_slug(request.args.get("scope"))
    if requested in {"municipio", "pyme", "colegio"}:
        return requested
    return _tenant_scope(tenant)


def _iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalized_state(value: Any) -> str:
    return str(value or "nuevo").strip().lower()


def _is_closed_state(value: Any) -> bool:
    return _normalized_state(value) in _CLOSED_STATES


def _label(value: Any, *, fallback: str = "Sin dato") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return text.replace("_", " ").capitalize()


def _employees_for_tenant(tenant: TenantProfile) -> list[User]:
    clauses = [User.tenant_id == tenant.id]
    if tenant.municipio_id:
        clauses.extend(
            [
                User.id == tenant.municipio_id,
                User.empresa_id == tenant.municipio_id,
                User.municipio_id == tenant.municipio_id,
            ]
        )
    if tenant.pyme_id:
        clauses.extend(
            [
                User.id == tenant.pyme_id,
                User.empresa_id == tenant.pyme_id,
                User.pyme_id == tenant.pyme_id,
            ]
        )
    seen: set[int] = set()
    employees: list[User] = []
    for user in User.query.filter(or_(*clauses)).order_by(User.id.asc()).all():
        if user.id in seen:
            continue
        seen.add(user.id)
        employees.append(user)
    return employees


def _agent_summary(user: User | None, *, workload: int | None = None) -> dict[str, Any] | None:
    if not user:
        return None
    payload = {
        "id": user.id,
        "label": user.name,
        "email": user.email,
        "role": str(getattr(user, "rol", "") or "usuario"),
    }
    if workload is not None:
        payload["workload"] = workload
    return payload


def _ticket_records_for(tenant: TenantProfile, scope: str) -> tuple[str, list[Any]]:
    use_pyme = scope == "pyme" or bool(tenant.pyme_id and not tenant.municipio_id)
    if use_pyme:
        records = (
            PymeTicket.query.filter(PymeTicket.tenant_id == tenant.id)
            .order_by(PymeTicket.fecha.desc(), PymeTicket.id.desc())
            .all()
        )
        return "pyme", records

    clauses = [MunicipioTicket.tenant_id == tenant.id]
    if tenant.municipio_id:
        clauses.append(MunicipioTicket.municipio_id == tenant.municipio_id)
    records = (
        MunicipioTicket.query.filter(or_(*clauses))
        .order_by(MunicipioTicket.fecha.desc(), MunicipioTicket.id.desc())
        .all()
    )
    return "municipio", records


def _ticket_channel(ticket: Any) -> str | None:
    return getattr(ticket, "canal_ingreso", None) or getattr(ticket, "channel", None) or None


def _ticket_priority(ticket: Any) -> str | None:
    value = getattr(ticket, "prioridad", None) or getattr(ticket, "priority", None)
    return str(value).strip() if value else None


def _ticket_sla_status(ticket: Any, now: datetime) -> str:
    if _is_closed_state(getattr(ticket, "estado", None)):
        return "closed"
    references = [
        value
        for value in (
            _as_utc(getattr(ticket, "fecha", None)),
            _as_utc(getattr(ticket, "ultima_actividad", None)),
        )
        if value
    ]
    reference = min(references) if references else None
    if not reference:
        return "unknown"
    age = now - reference
    if age >= timedelta(hours=48):
        return "overdue"
    if age >= timedelta(hours=24):
        return "risk"
    return "ok"


def _count_unread_tickets(ticket_type: str, tickets: list[Any], current_user: User) -> int:
    unread = 0
    for ticket in tickets:
        if ticket_type == "municipio":
            latest_comment = (
                TicketComentario.query.filter(
                    TicketComentario.municipio_ticket_id == ticket.id,
                    TicketComentario.es_admin.is_(False),
                )
                .order_by(TicketComentario.id.desc())
                .first()
            )
        else:
            latest_comment = (
                TicketComentario.query.filter(
                    TicketComentario.pyme_ticket_id == ticket.id,
                    TicketComentario.es_admin.is_(False),
                )
                .order_by(TicketComentario.id.desc())
                .first()
            )
        if not latest_comment:
            continue
        read_state = (
            TicketRealtimeState.query.filter(
                TicketRealtimeState.ticket_type == ticket_type,
                TicketRealtimeState.ticket_id == ticket.id,
                TicketRealtimeState.viewer_user_id == current_user.id,
            )
            .order_by(TicketRealtimeState.id.desc())
            .first()
        )
        if not read_state or not read_state.last_read_comment_id or latest_comment.id > read_state.last_read_comment_id:
            unread += 1
    return unread


def _counter_filter(items: list[Any], value_getter, *, fallback: str = "Sin dato") -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for item in items:
        raw = value_getter(item)
        key = str(raw or "").strip().lower() or fallback.lower().replace(" ", "_")
        counts[key] = counts.get(key, 0) + 1
        labels[key] = _label(raw, fallback=fallback)
    return [
        {"id": key, "label": labels[key], "count": count}
        for key, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _ticket_allowed_actions(ticket: Any) -> list[str]:
    if _is_closed_state(getattr(ticket, "estado", None)):
        return ["view", "reopen", "export"]
    actions = ["view", "add_internal_note", "change_status"]
    if not getattr(ticket, "asignado_a_id", None):
        actions.append("assign")
    actions.append("resolve")
    return actions


def _ticket_recommended_action(ticket: Any, sla_status: str) -> dict[str, Any] | None:
    if _is_closed_state(getattr(ticket, "estado", None)):
        return None
    if not getattr(ticket, "asignado_a_id", None):
        return {"id": "assign_owner", "label": "Asignar responsable", "reason": "El caso esta abierto y no tiene responsable."}
    if sla_status in {"risk", "overdue"}:
        return {"id": "review_sla", "label": "Revisar SLA", "reason": "El caso esta vencido o por vencer."}
    return {"id": "continue_case", "label": "Dar proximo paso", "reason": "El caso sigue abierto."}


def _ticket_item(ticket_type: str, ticket: Any, *, now: datetime, request_id: str) -> dict[str, Any]:
    sla_status = _ticket_sla_status(ticket, now)
    number = getattr(ticket, "nro_ticket", None) or ticket.id
    detail_base = "municipio" if ticket_type == "municipio" else "pyme"
    return {
        "id": ticket.id,
        "request_id": request_id,
        "type": ticket_type,
        "number": str(number),
        "title": getattr(ticket, "asunto", None) or getattr(ticket, "pregunta", None) or f"Ticket {number}",
        "status": getattr(ticket, "estado", None) or "nuevo",
        "area": getattr(ticket, "categoria", None),
        "channel": _ticket_channel(ticket),
        "created_at": _iso(getattr(ticket, "fecha", None)),
        "detail_endpoint": f"/tickets/{detail_base}/{ticket.id}",
        "allowed_actions": _ticket_allowed_actions(ticket),
        "sla_status": sla_status,
        "priority": _ticket_priority(ticket),
        "assigned_agent": _agent_summary(getattr(ticket, "asignado_a", None)),
        "recommended_next_action": _ticket_recommended_action(ticket, sla_status),
    }


def _inbox_summary_payload(current_user: User, tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    scope = _requested_scope(tenant)
    ticket_type, tickets = _ticket_records_for(tenant, scope)
    now = datetime.now(timezone.utc)
    open_tickets = [ticket for ticket in tickets if not _is_closed_state(getattr(ticket, "estado", None))]
    resolved = len(tickets) - len(open_tickets)
    sla_counts: dict[str, int] = {}
    for ticket in tickets:
        status = _ticket_sla_status(ticket, now)
        sla_counts[status] = sla_counts.get(status, 0) + 1
    unassigned = sum(1 for ticket in open_tickets if not getattr(ticket, "asignado_a_id", None))
    unread = _count_unread_tickets(ticket_type, tickets, current_user)
    sla_risk = sum(1 for ticket in open_tickets if _ticket_sla_status(ticket, now) in {"risk", "overdue"})
    employees = _employees_for_tenant(tenant)
    workload_by_agent: dict[int, int] = {}
    for ticket in open_tickets:
        agent_id = getattr(ticket, "asignado_a_id", None)
        if agent_id:
            workload_by_agent[int(agent_id)] = workload_by_agent.get(int(agent_id), 0) + 1

    recommended_views: list[dict[str, Any]] = []
    if sla_risk:
        recommended_views.append(
            {
                "id": "sla_risk",
                "label": "Riesgo SLA",
                "description": "Casos vencidos o por vencer.",
                "query": {"sla": "risk"},
            }
        )
    if unassigned:
        recommended_views.append(
            {
                "id": "unassigned",
                "label": "Sin responsable",
                "description": "Casos abiertos sin agente asignado.",
                "query": {"assigned": "none"},
            }
        )
    if unread:
        recommended_views.append(
            {
                "id": "unread",
                "label": "Sin leer",
                "description": "Casos con actividad ciudadana o de cliente sin lectura registrada.",
                "query": {"unread": True},
            }
        )

    sorted_items = sorted(
        open_tickets,
        key=lambda ticket: (
            0 if _ticket_sla_status(ticket, now) == "overdue" else 1 if _ticket_sla_status(ticket, now) == "risk" else 2,
            getattr(ticket, "fecha", None) or datetime.min,
        ),
    )

    return {
        "contract_version": "backoffice.inbox_summary.v1",
        "request_id": request_id,
        "tenant_slug": tenant.slug,
        "scope": scope,
        "summary": {
            "total": len(tickets),
            "open": len(open_tickets),
            "unread": unread,
            "sla_risk": sla_risk,
            "resolved": resolved,
            "unassigned": unassigned,
        },
        "filters": {
            "channels": _counter_filter([ticket for ticket in tickets if _ticket_channel(ticket)], _ticket_channel),
            "statuses": _counter_filter(tickets, lambda ticket: getattr(ticket, "estado", None)),
            "areas": _counter_filter([ticket for ticket in tickets if getattr(ticket, "categoria", None)], lambda ticket: getattr(ticket, "categoria", None)),
            "agents": [
                agent
                for agent in (
                    _agent_summary(employee, workload=workload_by_agent.get(employee.id, 0))
                    for employee in employees
                )
                if agent
            ],
            "priorities": _counter_filter([ticket for ticket in tickets if _ticket_priority(ticket)], _ticket_priority),
            "sla_statuses": [
                {"id": key, "label": _label(key), "count": count}
                for key, count in sorted(sla_counts.items(), key=lambda pair: (-pair[1], pair[0]))
            ],
        },
        "recommended_views": recommended_views,
        "items": [_ticket_item(ticket_type, ticket, now=now, request_id=request_id) for ticket in sorted_items[:25]],
    }


def _orders_for_tenant(tenant: TenantProfile) -> list[PymePedido]:
    clauses = [PymePedido.tenant_id == tenant.id]
    if tenant.pyme_id:
        clauses.append(PymePedido.pyme_id == tenant.pyme_id)
    return (
        PymePedido.query.filter(or_(*clauses))
        .order_by(PymePedido.fecha.desc(), PymePedido.id.desc())
        .all()
    )


def _order_actions_for_status(status: str) -> list[str]:
    state = _normalized_state(status)
    if state in {"cancelado", "cancelada"}:
        return ["view", "export"]
    if state in _ORDER_FINAL_STATES:
        return ["view", "export", "send_receipt"]
    return ["view", "confirm_payment", "change_status", "assign_dispatch", "cancel"]


def _orders_summary_payload(tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    orders = _orders_for_tenant(tenant)
    active = [order for order in orders if _normalized_state(order.estado) not in _ORDER_FINAL_STATES]
    finalized = [order for order in orders if _normalized_state(order.estado) in _ORDER_FINAL_STATES]
    confirmed_revenue = sum(float(order.monto_total or 0) for order in orders if _normalized_state(order.estado) in _ORDER_CONFIRMED_REVENUE_STATES)
    pending_revenue = sum(float(order.monto_total or 0) for order in active if _normalized_state(order.estado) not in _ORDER_CONFIRMED_REVENUE_STATES)
    statuses = _counter_filter(orders, lambda order: getattr(order, "estado", None), fallback="Sin estado")
    data_quality_notes: list[str] = []
    if orders and not hasattr(PymePedido, "asignado_a_id"):
        data_quality_notes.append("Los pedidos aun no tienen campo de responsable; no se publica un conteo inventado de responsables.")

    return {
        "contract_version": "backoffice.orders_summary.v1",
        "request_id": request_id,
        "tenant_slug": tenant.slug,
        "summary": {
            "total": len(orders),
            "active": len(active),
            "finalized": len(finalized),
            "confirmed_revenue": round(confirmed_revenue, 2),
            "pending_revenue": round(pending_revenue, 2),
            "unassigned": None,
        },
        "statuses": statuses,
        "active_orders": [
            {
                "id": order.id,
                "number": order.nro_pedido,
                "status": order.estado,
                "total": order.monto_total,
                "customer": order.nombre_cliente,
                "created_at": _iso(order.fecha),
                "detail_endpoint": f"/api/public/tracking/experience?kind=order&code={order.nro_pedido}",
                "allowed_actions": _order_actions_for_status(order.estado),
            }
            for order in active[:25]
        ],
        "actions_by_status": {
            item["id"]: _order_actions_for_status(item["id"])
            for item in statuses
        },
        "data_quality_notes": data_quality_notes,
    }


def _contact_key(contact: dict[str, Any]) -> str:
    email = str(contact.get("email") or "").strip().lower()
    phone = "".join(ch for ch in str(contact.get("phone") or "") if ch.isdigit())
    if email:
        return f"email:{email}"
    if phone:
        return f"phone:{phone}"
    name = str(contact.get("name") or "").strip().lower()
    if name:
        return f"name:{name}"
    return f"anon:{contact.get('source')}:{contact.get('source_id')}"


def _collect_contacts(tenant: TenantProfile) -> list[dict[str, Any]]:
    contacts: dict[str, dict[str, Any]] = {}

    def add_contact(raw: dict[str, Any]) -> None:
        key = _contact_key(raw)
        current = contacts.setdefault(
            key,
            {
                "key": key,
                "name": raw.get("name"),
                "email": raw.get("email"),
                "phone": raw.get("phone"),
                "channels": set(),
                "sources": set(),
                "accepts_marketing": False,
                "records": 0,
            },
        )
        if raw.get("name") and not current.get("name"):
            current["name"] = raw.get("name")
        if raw.get("email") and not current.get("email"):
            current["email"] = raw.get("email")
        if raw.get("phone") and not current.get("phone"):
            current["phone"] = raw.get("phone")
        if raw.get("channel"):
            current["channels"].add(str(raw.get("channel")))
        if raw.get("source"):
            current["sources"].add(str(raw.get("source")))
        current["accepts_marketing"] = bool(current["accepts_marketing"] or raw.get("accepts_marketing"))
        current["records"] = int(current["records"] or 0) + 1

    ticket_type, tickets = _ticket_records_for(tenant, _tenant_scope(tenant))
    for ticket in tickets:
        if ticket_type == "municipio":
            add_contact(
                {
                    "source": "ticket",
                    "source_id": ticket.id,
                    "name": getattr(ticket, "nombre_vecino", None) or getattr(ticket, "nombre_display_whatsapp", None),
                    "email": getattr(ticket, "email_vecino", None),
                    "phone": getattr(ticket, "telefono_vecino", None),
                    "channel": _ticket_channel(ticket),
                }
            )
        else:
            add_contact(
                {
                    "source": "ticket",
                    "source_id": ticket.id,
                    "name": None,
                    "email": getattr(ticket, "email", None),
                    "phone": getattr(ticket, "telefono", None),
                    "channel": _ticket_channel(ticket),
                }
            )

    for order in _orders_for_tenant(tenant):
        add_contact(
            {
                "source": "order",
                "source_id": order.id,
                "name": order.nombre_cliente,
                "email": order.email_cliente,
                "phone": order.telefono_cliente,
                "channel": "pedido",
            }
        )

    for user in _employees_for_tenant(tenant):
        add_contact(
            {
                "source": "user",
                "source_id": user.id,
                "name": user.name,
                "email": user.email,
                "phone": user.telefono,
                "channel": "panel",
                "accepts_marketing": bool(getattr(user, "acepta_marketing", False)),
            }
        )

    normalized: list[dict[str, Any]] = []
    for contact in contacts.values():
        normalized.append(
            {
                **contact,
                "channels": sorted(contact["channels"]),
                "sources": sorted(contact["sources"]),
            }
        )
    return normalized


def _segment_counts(values: list[str]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        key = str(value).strip().lower()
        counts[key] = counts.get(key, 0) + 1
    return [
        {"id": key, "label": _label(key), "count": count}
        for key, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _contacts_summary_payload(tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    contacts = _collect_contacts(tenant)
    all_channels = [channel for contact in contacts for channel in contact["channels"]]
    all_sources = [source for contact in contacts for source in contact["sources"]]
    duplicates = [contact for contact in contacts if int(contact.get("records") or 0) > 1]
    missing_minimum = [
        contact
        for contact in contacts
        if not str(contact.get("email") or "").strip() and not str(contact.get("phone") or "").strip()
    ]
    return {
        "contract_version": "backoffice.contacts_summary.v1",
        "request_id": request_id,
        "tenant_slug": tenant.slug,
        "summary": {
            "total": len(contacts),
            "with_phone": sum(1 for contact in contacts if str(contact.get("phone") or "").strip()),
            "with_email": sum(1 for contact in contacts if str(contact.get("email") or "").strip()),
            "opt_in_marketing": sum(1 for contact in contacts if contact.get("accepts_marketing")),
            "possible_duplicates": len(duplicates),
            "missing_minimum_data": len(missing_minimum),
        },
        "main_channels": _segment_counts(all_channels),
        "segments": {
            "channels": _segment_counts(all_channels),
            "sources": _segment_counts(all_sources),
            "has_phone": [
                {"id": "yes", "label": "Con telefono", "count": sum(1 for contact in contacts if contact.get("phone"))},
                {"id": "no", "label": "Sin telefono", "count": sum(1 for contact in contacts if not contact.get("phone"))},
            ],
            "has_email": [
                {"id": "yes", "label": "Con email", "count": sum(1 for contact in contacts if contact.get("email"))},
                {"id": "no", "label": "Sin email", "count": sum(1 for contact in contacts if not contact.get("email"))},
            ],
        },
        "contacts_missing_minimum": [
            {"key": contact["key"], "name": contact.get("name"), "sources": contact.get("sources", [])}
            for contact in missing_minimum[:25]
        ],
        "possible_duplicates": [
            {"key": contact["key"], "records": contact.get("records"), "sources": contact.get("sources", [])}
            for contact in duplicates[:25]
        ],
    }


def _employee_categories(user: User) -> list[str]:
    categories: set[str] = set()
    for category in getattr(user, "categorias_lista", []) or []:
        if category:
            categories.add(str(category).strip())
    for category in getattr(user, "categorias_ticket", []) or []:
        name = getattr(category, "nombre", None)
        if name:
            categories.add(str(name).strip())
    return sorted(category for category in categories if category)


def _team_coverage_payload(tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    scope = _tenant_scope(tenant)
    ticket_type, tickets = _ticket_records_for(tenant, scope)
    open_tickets = [ticket for ticket in tickets if not _is_closed_state(getattr(ticket, "estado", None))]
    employees = _employees_for_tenant(tenant)
    workload_by_agent: dict[int, int] = {}
    categories_by_agent: dict[int, set[str]] = {}
    channels_by_agent: dict[int, set[str]] = {}

    for ticket in open_tickets:
        agent_id = getattr(ticket, "asignado_a_id", None)
        if not agent_id:
            continue
        workload_by_agent[int(agent_id)] = workload_by_agent.get(int(agent_id), 0) + 1
        category = getattr(ticket, "categoria", None)
        channel = _ticket_channel(ticket)
        if category:
            categories_by_agent.setdefault(int(agent_id), set()).add(str(category))
        if channel:
            channels_by_agent.setdefault(int(agent_id), set()).add(str(channel))

    configured_categories = {category for employee in employees for category in _employee_categories(employee)}
    open_categories = {str(getattr(ticket, "categoria", "")).strip() for ticket in open_tickets if getattr(ticket, "categoria", None)}
    assigned_open_categories = {
        str(getattr(ticket, "categoria", "")).strip()
        for ticket in open_tickets
        if getattr(ticket, "categoria", None) and getattr(ticket, "asignado_a_id", None)
    }
    covered_categories = sorted((configured_categories | assigned_open_categories) & open_categories)
    uncovered_categories = sorted(open_categories - set(covered_categories))
    recommendations: list[dict[str, Any]] = []
    for category in uncovered_categories[:5]:
        recommendations.append(
            {
                "id": f"assign_category_{category.lower().replace(' ', '_')}",
                "label": f"Asignar responsable para {category}",
                "description": "Hay casos abiertos en esta categoria sin cobertura visible.",
                "query": {"category": category, "assigned": "none"},
            }
        )
    overloaded = [employee for employee in employees if workload_by_agent.get(employee.id, 0) >= 10]
    for employee in overloaded[:5]:
        recommendations.append(
            {
                "id": f"rebalance_agent_{employee.id}",
                "label": f"Revisar carga de {employee.name}",
                "description": "El agente concentra muchos casos abiertos.",
                "query": {"agent_id": employee.id},
            }
        )

    return {
        "contract_version": "backoffice.team_coverage_summary.v1",
        "request_id": request_id,
        "tenant_slug": tenant.slug,
        "scope": scope,
        "summary": {
            "active_employees": len(employees),
            "covered_categories": len(covered_categories),
            "uncovered_categories": len(uncovered_categories),
            "covered_zones": 0,
            "covered_channels": len({channel for channels in channels_by_agent.values() for channel in channels}),
        },
        "employees": [
            {
                **(_agent_summary(employee, workload=workload_by_agent.get(employee.id, 0)) or {}),
                "editable_scope": {
                    "categories": _employee_categories(employee),
                    "role": str(getattr(employee, "rol", "") or "usuario"),
                    "tenant_id": getattr(employee, "tenant_id", None),
                },
                "categories_covered": sorted(categories_by_agent.get(employee.id, set()) | set(_employee_categories(employee))),
                "channels_covered": sorted(channels_by_agent.get(employee.id, set())),
            }
            for employee in employees
        ],
        "categories_covered": [{"id": category.lower(), "label": category} for category in covered_categories],
        "categories_without_owner": [{"id": category.lower(), "label": category} for category in uncovered_categories],
        "zones_covered": [],
        "channels_covered": _segment_counts([channel for channels in channels_by_agent.values() for channel in channels]),
        "workload_by_agent": [
            _agent_summary(employee, workload=workload_by_agent.get(employee.id, 0))
            for employee in employees
            if _agent_summary(employee, workload=workload_by_agent.get(employee.id, 0))
        ],
        "assignment_recommendations": recommendations,
        "permissions": {
            "can_edit_team": str(getattr(tenant, "plan", "") or "").lower() in {"pro", "enterprise"} or str(getattr(tenant, "plan", "") or "").lower().startswith("pro"),
            "editable_scopes": ["categories", "channels", "roles"],
        },
        "data_quality_notes": [] if ticket_type in {"municipio", "pyme"} else ["No hay modelo operativo especifico para este scope."],
    }


def _export_rows(resource: str, tenant: TenantProfile, current_user: User, request_id: str) -> list[dict[str, Any]]:
    if resource == "tickets":
        payload = _inbox_summary_payload(current_user, tenant, request_id)
        return payload.get("items", [])
    if resource == "orders":
        payload = _orders_summary_payload(tenant, request_id)
        return payload.get("active_orders", [])
    if resource == "contacts":
        return _collect_contacts(tenant)
    if resource == "team":
        payload = _team_coverage_payload(tenant, request_id)
        return payload.get("employees", [])
    return []


def _flatten_for_export(row: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dict, list, set, tuple)):
            flattened[key] = str(value)
        else:
            flattened[key] = value
    return flattened


def _export_dir() -> Path:
    static_folder = Path(current_app.static_folder or (Path(current_app.root_path) / "static"))
    path = static_folder / "exports" / "backoffice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv_export(path: Path, rows: list[dict[str, Any]]) -> None:
    normalized_rows = [_flatten_for_export(row) for row in rows]
    fieldnames = sorted({key for row in normalized_rows for key in row.keys()}) or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in normalized_rows:
            writer.writerow(row)


def _pdf_escape(text: Any) -> str:
    return str(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_pdf_export(path: Path, title: str, rows: list[dict[str, Any]]) -> None:
    lines = [title, f"Filas: {len(rows)}"]
    for row in rows[:20]:
        label = row.get("title") or row.get("number") or row.get("email") or row.get("label") or row.get("name") or row.get("id")
        lines.append(str(label))
    stream = "BT /F1 12 Tf 72 760 Td " + " ".join(
        f"({_pdf_escape(line)}) Tj 0 -18 Td" for line in lines[:35]
    ) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('utf-8'))} >>\nstream\n{stream}\nendstream",
    ]
    output = "%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output.encode("utf-8")))
        output += f"{index} 0 obj\n{obj}\nendobj\n"
    xref_offset = len(output.encode("utf-8"))
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n"
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    path.write_bytes(output.encode("utf-8"))


def _write_xlsx_export(path: Path, rows: list[dict[str, Any]]) -> None:
    from openpyxl import Workbook

    normalized_rows = [_flatten_for_export(row) for row in rows]
    fieldnames = sorted({key for row in normalized_rows for key in row.keys()}) or ["empty"]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "backoffice"
    sheet.append(fieldnames)
    for row in normalized_rows:
        sheet.append([row.get(field) for field in fieldnames])
    workbook.save(path)


def _build_download_url(filename: str) -> str:
    base = request.host_url.rstrip("/")
    return f"{base}/static/exports/backoffice/{filename}"


def _executive_summary_payload(tenant: TenantProfile, current_user: User, request_id: str) -> dict[str, Any]:
    inbox = _inbox_summary_payload(current_user, tenant, request_id)
    orders = _orders_summary_payload(tenant, request_id)
    contacts = _contacts_summary_payload(tenant, request_id)
    team = _team_coverage_payload(tenant, request_id)
    total_signals = int(inbox["summary"]["total"]) + int(orders["summary"]["total"]) + int(contacts["summary"]["total"])
    if total_signals >= 30:
        confidence = "high"
    elif total_signals >= 8:
        confidence = "medium"
    else:
        confidence = "low"

    risks: list[dict[str, Any]] = []
    if inbox["summary"]["sla_risk"]:
        risks.append({"id": "sla_risk", "label": "Casos en riesgo SLA", "value": inbox["summary"]["sla_risk"]})
    if inbox["summary"]["unassigned"]:
        risks.append({"id": "unassigned_cases", "label": "Casos sin responsable", "value": inbox["summary"]["unassigned"]})
    if contacts["summary"]["missing_minimum_data"]:
        risks.append({"id": "contacts_missing_data", "label": "Contactos incompletos", "value": contacts["summary"]["missing_minimum_data"]})

    opportunities: list[dict[str, Any]] = []
    if orders["summary"]["pending_revenue"]:
        opportunities.append({"id": "pending_revenue", "label": "Ingresos pendientes de confirmar", "value": orders["summary"]["pending_revenue"]})
    if contacts["summary"]["with_phone"]:
        opportunities.append({"id": "phone_followup", "label": "Contactos con telefono para seguimiento", "value": contacts["summary"]["with_phone"]})

    recommended_actions = [
        {"id": "open_inbox", "label": "Atender bandeja priorizada", "endpoint": "/api/v2/backoffice/operations/inbox-summary"},
    ]
    if team["categories_without_owner"]:
        recommended_actions.append({"id": "assign_coverage", "label": "Asignar categorias sin responsable", "endpoint": "/api/v2/backoffice/team/coverage-summary"})
    if contacts["summary"]["missing_minimum_data"]:
        recommended_actions.append({"id": "complete_contacts", "label": "Completar datos de contactos", "endpoint": "/api/v2/backoffice/contacts/summary"})

    data_quality_notes: list[str] = []
    if confidence == "low":
        data_quality_notes.append("Muestra insuficiente para conclusiones fuertes; mostrar como lectura inicial.")
    data_quality_notes.extend(orders.get("data_quality_notes") or [])

    return {
        "contract_version": "backoffice.executive_summary.v1",
        "request_id": request_id,
        "tenant_slug": tenant.slug,
        "headline": f"{inbox['summary']['open']} casos abiertos, {inbox['summary']['sla_risk']} en riesgo SLA y {orders['summary']['active']} pedidos activos.",
        "risks": risks,
        "opportunities": opportunities,
        "recommended_actions": recommended_actions,
        "confidence": confidence,
        "data_quality_notes": data_quality_notes,
        "source_endpoints": [
            "/api/v2/backoffice/operations/inbox-summary",
            "/api/v2/backoffice/orders/summary",
            "/api/v2/backoffice/contacts/summary",
            "/api/v2/backoffice/team/coverage-summary",
        ],
    }


@backoffice_v2_bp.get("/operations/inbox-summary")
@token_requerido
def backoffice_v2_inbox_summary(current_user: User):
    request_id = _request_id()
    tenant, error = _authorized_tenant_or_response(current_user, request_id)
    if error:
        return error
    return _json(_inbox_summary_payload(current_user, tenant, request_id), request_id=request_id)


@backoffice_v2_bp.get("/orders/summary")
@token_requerido
def backoffice_v2_orders_summary(current_user: User):
    request_id = _request_id()
    tenant, error = _authorized_tenant_or_response(current_user, request_id)
    if error:
        return error
    return _json(_orders_summary_payload(tenant, request_id), request_id=request_id)


@backoffice_v2_bp.get("/contacts/summary")
@token_requerido
def backoffice_v2_contacts_summary(current_user: User):
    request_id = _request_id()
    tenant, error = _authorized_tenant_or_response(current_user, request_id)
    if error:
        return error
    return _json(_contacts_summary_payload(tenant, request_id), request_id=request_id)


@backoffice_v2_bp.get("/team/coverage-summary")
@token_requerido
def backoffice_v2_team_coverage_summary(current_user: User):
    request_id = _request_id()
    tenant, error = _authorized_tenant_or_response(current_user, request_id)
    if error:
        return error
    return _json(_team_coverage_payload(tenant, request_id), request_id=request_id)


@backoffice_v2_bp.post("/export")
@token_requerido
def backoffice_v2_export(current_user: User):
    request_id = _request_id()
    body = request.get_json(silent=True) or {}
    tenant, error = _authorized_tenant_or_response(current_user, request_id, explicit_slug=body.get("tenant_slug"))
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, request_id, "analytics_dashboard")
    resource = _normalize_slug(body.get("resource"))
    export_format = _normalize_slug(body.get("format") or "csv")
    if resource not in {"tickets", "orders", "contacts", "team"}:
        return _json(
            {"ok": False, "reason_code": "invalid_resource", "message": "Resource invalido para exportacion."},
            status=400,
            request_id=request_id,
        )
    if export_format not in {"pdf", "csv", "xlsx"}:
        return _json(
            {"ok": False, "reason_code": "invalid_format", "message": "Formato invalido para exportacion."},
            status=400,
            request_id=request_id,
        )
    rows = _export_rows(resource, tenant, current_user, request_id)
    filename = f"{request_id}_{resource}.{export_format}"
    path = _export_dir() / filename
    try:
        if export_format == "csv":
            _write_csv_export(path, rows)
        elif export_format == "pdf":
            _write_pdf_export(path, f"Export {resource} - {tenant.slug}", rows)
        else:
            _write_xlsx_export(path, rows)
    except ImportError:
        return _json(
            {
                "ok": False,
                "reason_code": "export_dependency_missing",
                "message": "No esta disponible la dependencia para generar XLSX.",
            },
            status=501,
            request_id=request_id,
        )

    return _json(
        {
            "ok": True,
            "request_id": request_id,
            "download_url": _build_download_url(filename),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "resource": resource,
            "format": export_format,
            "filters": body.get("filters") or {},
        },
        request_id=request_id,
    )


@backoffice_v2_bp.post("/executive-summary")
@token_requerido
def backoffice_v2_executive_summary(current_user: User):
    request_id = _request_id()
    body = request.get_json(silent=True) or {}
    tenant, error = _authorized_tenant_or_response(current_user, request_id, explicit_slug=body.get("tenant_slug"))
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, request_id, "analytics_dashboard")
    return _json(_executive_summary_payload(tenant, current_user, request_id), request_id=request_id)
