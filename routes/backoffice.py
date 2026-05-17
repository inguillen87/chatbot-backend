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
from utils.auth_helpers import token_requerido

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
    return str(value or "").strip().lower()


def _resolve_tenant(current_user: User) -> TenantProfile | None:
    slug = _normalize_slug(
        request.args.get("tenant_slug")
        or request.args.get("tenant")
        or getattr(current_user, "tenant_slug", None)
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
    role = str(getattr(current_user, "rol", "") or "").strip().lower()
    if role == "super_admin":
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


def _analytics_modes(tenant: TenantProfile, current_user: User) -> dict[str, Any]:
    capabilities = _capabilities(tenant)
    plan = str(tenant.plan or "").strip().lower()
    role = str(getattr(current_user, "rol", "") or "").strip().lower()
    advanced_default = plan not in {"", "free", "gratis"} or role == "super_admin"
    advanced_enabled = _capability_enabled(
        capabilities,
        "advanced_analytics",
        default=advanced_default,
    )
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
    role = str(getattr(current_user, "rol", "") or "usuario").strip().lower() or "usuario"
    scope = _tenant_scope(tenant)
    capabilities = _capabilities(tenant)
    counts = counts or {"geo_points": 0}
    people_enabled = role in {"admin", "super_admin", "operador"} and _capability_enabled(capabilities, "people", default=True)
    surveys_enabled = _capability_enabled(capabilities, "surveys", default=True)
    maps_default = scope in {"municipio", "colegio"} or bool(counts.get("geo_points"))
    maps_enabled = _capability_enabled(capabilities, "maps", default=maps_default)

    modules = [
        {
            "id": "operations",
            "label": "Operar reclamos" if scope == "municipio" else "Operar casos y pedidos",
            "description": "Reclamos, estados, ubicaciones y seguimiento diario." if scope == "municipio" else "Casos, pedidos, estados y seguimiento diario.",
            "route": "/perfil?tab=tickets",
            "enabled": _capability_enabled(capabilities, "operations", default=True),
            "priority": 1,
        },
        {
            "id": "reports",
            "label": "Reportes claros",
            "description": "Resumen operativo, mapas de calor y prioridades.",
            "route": "/perfil?tab=estadisticas",
            "enabled": bool((analytics_modes.get("statistics") or {}).get("enabled")),
            "priority": 2,
        },
        {
            "id": "surveys",
            "label": "Encuestas y sondeos",
            "description": "Participacion, votaciones, comentarios y resultados en vivo.",
            "route": "/admin/encuestas",
            "enabled": surveys_enabled,
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
            "priority": 5,
        },
        {
            "id": "advanced_analytics",
            "label": "Analitica IA",
            "description": "Segmentos, resumen ejecutivo, investigacion y exportaciones.",
            "route": "/analytics?mode=advanced",
            "enabled": bool((analytics_modes.get("advanced_analytics") or {}).get("enabled")),
            "priority": 6,
        },
    ]
    return sorted(modules, key=lambda item: int(item.get("priority") or 999))


def _navigation_payload(current_user: User, tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    analytics_modes = _analytics_modes(tenant, current_user)
    surveys = _surveys_overview(tenant)
    counts = _operations_counts(tenant, since=datetime.now(timezone.utc) - timedelta(days=7))
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
        "role": str(getattr(current_user, "rol", "") or "usuario").strip().lower() or "usuario",
        "modules": _modules_for(tenant, current_user, analytics_modes=analytics_modes, surveys=surveys, counts=counts),
        "analytics_modes": analytics_modes,
        "surveys_overview": surveys,
        "request_id": request_id,
    }


def _summary_payload(current_user: User, tenant: TenantProfile, request_id: str) -> dict[str, Any]:
    since, normalized_window = _parse_window(request.args.get("window"))
    counts = _operations_counts(tenant, since=since)
    surveys = _surveys_overview(tenant, since=since)
    analytics_modes = _analytics_modes(tenant, current_user)
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
