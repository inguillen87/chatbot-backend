from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from functools import wraps
from time import monotonic
from typing import Any
import uuid

from flask import Blueprint, Response, current_app, has_request_context, jsonify, request

import config.feature_flags as feature_flags
from cutover_writer_fence import cutover_writer_view
from models import TenantTicket
from routes import analytics_routes as legacy_analytics
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.analytics_service import analytics_service
from services.analytics.rbac import legacy_tenant_wide_analytics_denial
from services.ai_provider_status import build_ai_provider_status_public_view
from services.llm_orchestrator import build_llm_task_policy
from services.openai_bridge import generate_analytics_report
from services.operational_intelligence import (
    build_action_center,
    build_ai_ops_queue,
    build_operational_dashboard,
    build_operational_freshness,
    build_operational_heatmap,
)
from services.operational_heatmap_access import heatmap_viewer_cache_signature
from services.plan_access import (
    integration_access_payload,
    integration_feature_payload,
    integration_plan_required_payload,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_analytics_bp = Blueprint("v2_analytics", __name__, url_prefix="/api/v2/analytics")
_OPERATIONS_DASHBOARD_CACHE_TTL_SECONDS = 20
_OPERATIONS_DASHBOARD_CACHE_MAX_ENTRIES = 64
_OPERATIONS_DASHBOARD_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


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


def _legacy_tenant_wide_analytics_admin_only(fn):
    """Reject employees before route-local tenant-wide services or queries."""

    @wraps(fn)
    def _wrapped(current_user, *args, **kwargs):
        request_id = _request_id()
        denial = legacy_tenant_wide_analytics_denial(
            current_user,
            request_id=request_id,
        )
        if denial is not None:
            response = jsonify(denial)
            response.status_code = 403
            response.headers["X-Request-Id"] = request_id
            response.headers["Cache-Control"] = "no-store"
            return response
        return fn(current_user, *args, **kwargs)

    return _wrapped


def _operations_dashboard_cache_enabled() -> bool:
    if current_app.config.get("TESTING") and not current_app.config.get("ENABLE_OPERATIONS_DASHBOARD_CACHE_FOR_TESTS"):
        return False
    return bool(current_app.config.get("ENABLE_OPERATIONS_DASHBOARD_CACHE", True))


def _operation_cache_datetime(value: datetime) -> str:
    if hasattr(value, "timestamp"):
        try:
            bucket = int(value.timestamp() // _OPERATIONS_DASHBOARD_CACHE_TTL_SECONDS)
            return f"bucket:{bucket}"
        except (OSError, OverflowError, ValueError):
            pass
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _operations_dashboard_cache_key(
    tenant,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
) -> tuple[Any, ...]:
    viewer_signature = heatmap_viewer_cache_signature(viewer)
    if has_request_context():
        range_signature = tuple(
            (name, tuple(request.args.getlist(name)))
            for name in ("from", "to", "days", "range", "scope")
            if request.args.getlist(name)
        )
        return (
            getattr(tenant, "id", None),
            getattr(tenant, "slug", None),
            viewer_signature,
            "request_range",
            range_signature or (("default_days", "7"),),
        )

    return (
        getattr(tenant, "id", None),
        getattr(tenant, "slug", None),
        viewer_signature,
        _operation_cache_datetime(start_date),
        _operation_cache_datetime(end_date),
    )


def _prune_operations_dashboard_cache(now: float) -> None:
    stale_keys = [
        key
        for key, (created_at, _) in _OPERATIONS_DASHBOARD_CACHE.items()
        if now - created_at >= _OPERATIONS_DASHBOARD_CACHE_TTL_SECONDS
    ]
    for key in stale_keys:
        _OPERATIONS_DASHBOARD_CACHE.pop(key, None)

    overflow = len(_OPERATIONS_DASHBOARD_CACHE) - _OPERATIONS_DASHBOARD_CACHE_MAX_ENTRIES
    if overflow <= 0:
        return
    oldest_keys = sorted(_OPERATIONS_DASHBOARD_CACHE, key=lambda key: _OPERATIONS_DASHBOARD_CACHE[key][0])[:overflow]
    for key in oldest_keys:
        _OPERATIONS_DASHBOARD_CACHE.pop(key, None)


def _clear_operations_dashboard_cache_for_tests() -> None:
    _OPERATIONS_DASHBOARD_CACHE.clear()


def _cached_operational_dashboard(
    tenant,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
) -> dict[str, Any]:
    if not _operations_dashboard_cache_enabled():
        return build_operational_dashboard(tenant, start_date, end_date, viewer=viewer)

    now = monotonic()
    _prune_operations_dashboard_cache(now)
    key = _operations_dashboard_cache_key(tenant, start_date, end_date, viewer=viewer)
    cached = _OPERATIONS_DASHBOARD_CACHE.get(key)
    if cached and now - cached[0] < _OPERATIONS_DASHBOARD_CACHE_TTL_SECONDS:
        return deepcopy(cached[1])

    payload = build_operational_dashboard(tenant, start_date, end_date, viewer=viewer)
    _OPERATIONS_DASHBOARD_CACHE[key] = (now, deepcopy(payload))
    return payload


def _pdf_escape(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_text_pdf(lines: list[str]) -> bytes:
    y_start = 790
    line_height = 14
    chunks = ["BT /F1 10 Tf"]
    current_y = y_start
    for index, line in enumerate(lines):
        safe_line = _pdf_escape(line)
        if index == 0:
            chunks.append(f"50 {current_y} Td ({safe_line}) Tj")
        else:
            chunks.append(f"0 -{line_height} Td ({safe_line}) Tj")
        current_y -= line_height
        if current_y <= 40:
            break
    chunks.append("ET")
    stream = "\n".join(chunks).encode("latin-1", errors="replace")

    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        f"<</Length {len(stream)}>>stream\n".encode("ascii") + stream + b"\nendstream",
    ]

    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")

    xref_start = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(f"trailer<</Root 1 0 R/Size {len(offsets)}>>\n".encode("ascii"))
    body.extend(f"startxref\n{xref_start}\n%%EOF".encode("ascii"))
    return bytes(body)


def _tenant_analytics_segment(tenant) -> str:
    tipo = str(getattr(tenant, "tipo", "") or "").strip().lower()
    return "municipio" if tipo in {"municipio", "gobierno", "government"} else "pyme"


def _dashboard_report_lines(payload: dict[str, Any]) -> list[str]:
    tenant = payload.get("tenant") or {}
    scope = payload.get("scope") or {}
    summary = payload.get("summary") or {}
    ticket_section = payload.get("tickets") or {}
    survey_section = payload.get("surveys") or {}
    chat_section = payload.get("chats") or {}
    commerce_section = payload.get("commerce") or {}
    tickets = ticket_section.get("summary") or {}
    surveys = survey_section.get("summary") or {}
    chats = chat_section.get("summary") or {}
    commerce = commerce_section.get("summary") or {}
    maps = (((payload.get("maps") or {}).get("heatmap") or {}) or {})
    heatmap_summary = maps.get("summary") or {}
    location_quality = maps.get("location_quality") or {}
    alerts = payload.get("alerts") or []
    actions = payload.get("next_best_actions") or []

    def _section_value(section: dict[str, Any], values: dict[str, Any], key: str):
        if section.get("available") is False:
            return "no disponible por alcance"
        return values.get(key, 0)

    lines = [
        "Reporte operativo Chatboc",
        f"Tenant: {tenant.get('slug') or tenant.get('id') or 'sin_tenant'}",
        f"Alcance: {scope.get('mode') or 'tenant_wide'}",
        f"Emitido: {datetime.utcnow().isoformat()}Z",
        "",
        "Resumen:",
        f"- Tickets abiertos: {summary.get('open_tickets', 0)}",
        f"- Tickets vencidos: {summary.get('overdue_tickets', 0)}",
        f"- Respuestas encuestas: {_section_value(survey_section, surveys, 'responses')}",
        f"- Mensajes chat: {_section_value(chat_section, chats, 'messages')}",
        f"- Pedidos asistidos a revisar: {_section_value(commerce_section, commerce, 'orders_needing_review')}",
        "",
        "Tickets:",
        f"- Total: {tickets.get('total', 0)}",
        f"- Sin asignar: {tickets.get('unassigned', 0)}",
        f"- Con ubicacion: {tickets.get('with_location', 0)}",
        "",
        "Mapa operativo:",
        f"- Puntos: {heatmap_summary.get('points', 0)}",
        f"- Celdas: {heatmap_summary.get('cells', 0)}",
        f"- Pendientes geocodificar: {heatmap_summary.get('pending_geocode', 0)}",
        f"- Cobertura coordenadas: {location_quality.get('coordinate_coverage_pct', 0)}%",
        "",
        "Encuestas y chat:",
        f"- Votaciones live: {_section_value(survey_section, surveys, 'votaciones_live')}",
        f"- WhatsApp: {_section_value(chat_section, chats, 'whatsapp_messages')}",
        f"- Widget: {_section_value(chat_section, chats, 'widget_messages')}",
        "",
        "Marketplace y pedidos:",
        f"- Pedidos: {_section_value(commerce_section, commerce, 'orders')}",
        f"- Asistidos: {_section_value(commerce_section, commerce, 'assisted_orders')}",
        f"- Requieren revision: {_section_value(commerce_section, commerce, 'orders_needing_review')}",
        f"- Items sin resolver: {_section_value(commerce_section, commerce, 'unmatched_items')}",
        "",
        "Alertas:",
    ]
    if alerts:
        for alert in alerts[:5]:
            lines.append(f"- {alert.get('severity', 'info')}: {alert.get('reason_code')} - {alert.get('message')}")
    else:
        lines.append("- Sin alertas criticas")

    lines.extend(["", "Proximas acciones:"])
    if actions:
        for action in actions[:5]:
            lines.append(f"- {action.get('priority', 'low')}: {action.get('title')}")
    else:
        lines.append("- Mantener monitoreo")
    return lines


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


def _integration_access(tenant) -> dict[str, Any]:
    return integration_access_payload(tenant)


def _feature_access(access: dict[str, Any], feature_id: str) -> dict[str, Any]:
    return integration_feature_payload(access, feature_id)


def _feature_enabled(access: dict[str, Any], feature_id: str) -> bool:
    return bool(_feature_access(access, feature_id).get("enabled"))


def _with_access(payload: dict[str, Any], tenant) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("access", _integration_access(tenant))
    return body


def _integration_plan_required_response(tenant, feature_id: str):
    return _json_response(
        integration_plan_required_payload(
            tenant,
            feature_id,
            render_as="integration_locked_state",
        ),
        403,
    )


def _resolve_tenant_or_error(current_user):
    explicit_slug = (
        request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()
    if not explicit_slug:
        return None, _error_response("X-Tenant-Slug es obligatorio en analytics v2", 400, "missing_tenant", "send_tenant_slug")

    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=explicit_slug)
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "check_tenant_slug")

    role = str(getattr(current_user, "rol", "") or "").lower()
    if role != "super_admin":
        same_slug = (getattr(current_user, "tenant_slug", "") or "").strip().lower() == (tenant.slug or "").strip().lower()
        same_id = str(getattr(current_user, "tenant_id", "") or "") == str(tenant.id)
        if not same_slug and not same_id:
            return None, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")

    return tenant, None


def _date_range(default_days: int = 7):
    now = datetime.utcnow()
    default_start = now - timedelta(days=default_days)

    from_str = request.args.get("from")
    to_str = request.args.get("to")
    range_mode = (request.args.get("range") or request.args.get("scope") or "").strip().lower()
    days_str = request.args.get("days")

    start_date = default_start
    end_date = now

    if days_str:
        try:
            days = max(1, min(int(days_str), 3650))
            start_date = now - timedelta(days=days)
        except ValueError:
            pass

    if range_mode in {"all", "historical", "historico"}:
        start_date = datetime(1970, 1, 1)

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


def _csv_values(*names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        for raw in request.args.getlist(name):
            values.extend(part.strip() for part in str(raw or "").split(",") if part.strip())
    return values


def _heatmap_segment_filters() -> dict[str, list[str]]:
    filters = {
        "category": _csv_values("categoria", "categorias", "category", "categories"),
        "status": _csv_values("estado", "estados", "status", "statuses"),
        "gender": _csv_values("genero", "gender", "sexo"),
        "age_range": _csv_values("rango_edad", "age_range", "edad", "age"),
        "source": _csv_values("source", "fuente"),
        "channel": _csv_values("channel", "canal"),
        "zone": _csv_values("zona", "zonas", "zone", "barrio", "barrios", "distrito", "distritos"),
        "address": _csv_values("direccion", "direcciones", "address", "addresses"),
        "corridor": _csv_values("corredor", "corredores", "corridor", "corridors"),
        "sla_state": _csv_values("sla", "sla_state", "sla_status"),
        "assignee_id": _csv_values("assignee_id", "assigned_to", "agent", "agente"),
    }
    return {key: value for key, value in filters.items() if value}


def _request_bool_arg(name: str, default: bool = True) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "si", "on"}:
        return True
    return default


def _request_int_arg(*names: str, default: int, minimum: int, maximum: int) -> int:
    for name in names:
        raw = request.args.get(name)
        if raw in (None, ""):
            continue
        try:
            return max(minimum, min(int(raw), maximum))
        except (TypeError, ValueError):
            continue
    return default


def _heatmap_bbox_filter() -> dict[str, float] | None:
    raw_bbox = request.args.get("bbox") or request.args.get("bounds")
    values: list[float] = []

    if raw_bbox:
        try:
            values = [float(part.strip()) for part in str(raw_bbox).split(",") if part.strip()]
        except (TypeError, ValueError):
            values = []

    if len(values) != 4:
        try:
            west = request.args.get("west") or request.args.get("min_lng") or request.args.get("minLng")
            south = request.args.get("south") or request.args.get("min_lat") or request.args.get("minLat")
            east = request.args.get("east") or request.args.get("max_lng") or request.args.get("maxLng")
            north = request.args.get("north") or request.args.get("max_lat") or request.args.get("maxLat")
            values = [float(west), float(south), float(east), float(north)]
        except (TypeError, ValueError):
            return None

    west, south, east, north = values
    if west > east:
        west, east = east, west
    if south > north:
        south, north = north, south
    return {"west": west, "south": south, "east": east, "north": north}


@v2_analytics_bp.route("/overview", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@_legacy_tenant_wide_analytics_admin_only
def overview_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    start_date, end_date = _date_range()
    summary = analytics_service.get_summary(
        tenant_id=tenant.id,
        start_date=start_date,
        end_date=end_date,
        context=request.args.get("context", "overview"),
        filters={"channel": request.args.get("channel")} if request.args.get("channel") else {},
    )
    survey_summary = analytics_service.get_survey_summary(tenant.id) or {}
    survey_stats = survey_summary.get("stats") or {}
    conversations = int((summary.get("kpis") or {}).get("total_interactions") or 0)
    total_tickets = int(TenantTicket.query.filter_by(tenant_id=tenant.id).count())
    open_tickets = int(
        TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id, TenantTicket.estado.notin_(["cerrado", "closed"]))
        .count()
    )
    overdue_tickets = int(
        TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id, TenantTicket.estado.in_(["vencido", "overdue"])).count()
    )
    survey_responses = int(survey_stats.get("total_votes") or 0)
    survey_completion_rate = float(survey_stats.get("participation_rate") or 0.0)
    contract_summary = {
        "conversations": conversations,
        "open_tickets": open_tickets,
        "overdue_tickets": overdue_tickets,
        "response_time": 0,
        "survey_responses": survey_responses,
        "nps": 0,
        "csat": 0,
        "handoff_rate": 0,
    }

    return _json_response(
        {
            "contract_version": "analytics.overview.v2",
            "tenant_id": tenant.id,
            "summary": contract_summary,
            "total_conversations": conversations,
            "total_tickets": total_tickets,
            "open_tickets": open_tickets,
            "overdue_tickets": overdue_tickets,
            "avg_first_response_time": None,
            "avg_resolution_time": None,
            "survey_response_count": survey_responses,
            "survey_completion_rate": survey_completion_rate,
            "csat_score": 0,
            "nps_score": 0,
            "access": _integration_access(tenant),
        }
    )


@v2_analytics_bp.route("/tickets", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@_legacy_tenant_wide_analytics_admin_only
def tickets_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    start_date, end_date = _date_range()
    data = analytics_service.get_summary(
        tenant_id=tenant.id,
        start_date=start_date,
        end_date=end_date,
        context="municipio",
        filters={},
    )
    return _json_response(_with_access(data, tenant))


@v2_analytics_bp.route("/surveys", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def surveys_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    data = analytics_service.get_survey_summary(tenant.id)
    return _json_response(_with_access(data or {}, tenant))


@v2_analytics_bp.route("/funnel", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@_legacy_tenant_wide_analytics_admin_only
def funnel_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")
    start_date, end_date = _date_range()
    data = analytics_service.get_funnel_analytics(tenant.id, start_date, end_date)
    return _json_response(_with_access(data or {}, tenant))


@v2_analytics_bp.route("/operations", methods=["GET"])
@v2_analytics_bp.route("/operations/dashboard", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_dashboard_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    start_date, end_date = _date_range()
    payload = _cached_operational_dashboard(tenant, start_date, end_date, viewer=current_user)
    return _json_response(_with_access(payload, tenant))


@v2_analytics_bp.route("/operations/heatmap", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_heatmap_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "heatmaps"):
        return _integration_plan_required_response(tenant, "heatmaps")

    start_date, end_date = _date_range(default_days=365)
    include_ai = _request_bool_arg("include_ai", _request_bool_arg("ai", True))
    max_points = _request_int_arg("limit", "max_points", "maxPoints", default=1000, minimum=1, maximum=5000)
    payload = build_operational_heatmap(
        tenant,
        start_date,
        end_date,
        viewer=current_user,
        segment_filters=_heatmap_segment_filters(),
        include_ai=include_ai,
        max_points=max_points,
        bbox=_heatmap_bbox_filter(),
    )
    if (payload.get("privacy") or {}).get("mode") == "employee_aggregated":
        return _json_response(payload)
    return _json_response(_with_access(payload, tenant))


@v2_analytics_bp.route("/operations/action-center", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_action_center_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")

    start_date, end_date = _date_range()
    dashboard = _cached_operational_dashboard(tenant, start_date, end_date, viewer=current_user)
    payload = build_action_center(tenant, start_date, end_date, dashboard=dashboard)
    return _json_response(_with_access(payload, tenant))


@v2_analytics_bp.route("/operations/ai-brief", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_ai_brief_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")

    start_date, end_date = _date_range()
    dashboard = _cached_operational_dashboard(tenant, start_date, end_date, viewer=current_user)
    brief = dict(dashboard.get("ai_brief") or {})
    brief.setdefault("contract_version", "operations.ai_brief.v1")
    brief.update(
        {
            "tenant": dashboard.get("tenant"),
            "period": dashboard.get("period"),
            "generated_at": dashboard.get("generated_at"),
            "source_contract": dashboard.get("contract_version"),
            "scope": dashboard.get("scope") or {},
            "summary": dashboard.get("summary") or {},
            "alerts": dashboard.get("alerts") or [],
            "model_policy": build_llm_task_policy("analytics"),
            "access": _integration_access(tenant),
        }
    )
    return _json_response(brief)


@v2_analytics_bp.route("/operations/ai-provider-status", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_ai_provider_status_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")

    payload = build_ai_provider_status_public_view()
    payload.update(
        {
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "name": tenant.nombre,
                "type": tenant.tipo,
            },
            "model_policy": build_llm_task_policy("analytics"),
        }
    )
    return _json_response(_with_access(payload, tenant))


@v2_analytics_bp.route("/operations/ai-ops-queue", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_ai_ops_queue_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    if not feature_flags.FEATURE_AI_OPS_QUEUE:
        return _json_response(
            _with_access(
                {
                    "contract_version": "operations.ai_ops_queue.v1",
                    "enabled": False,
                    "reason_code": "feature_disabled",
                    "agent_display_name": "Valeria IA-Analytics",
                    "summary": {
                        "total": 0,
                        "high": 0,
                        "medium": 0,
                        "low": 0,
                        "tickets_needing_human": 0,
                        "orders_unmatched": 0,
                        "survey_alerts": 0,
                        "advisory_only": True,
                    },
                    "advisory_policy": {
                        "advisory_only": True,
                        "mutates_operational_state": False,
                        "requires_operator_confirmation": True,
                        "human_decision_required_for_critical_actions": True,
                        "external_ai_required": False,
                    },
                    "items": [],
                    "frontend_contract": {
                        "render_as": "ai_ops_queue",
                        "state": "disabled_by_feature_flag",
                        "empty_state_behavior": "hide_or_show_upgrade_preview",
                    },
                },
                tenant,
            )
        )

    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")

    try:
        limit = max(1, min(int(request.args.get("limit") or 15), 50))
    except (TypeError, ValueError):
        limit = 15

    start_date, end_date = _date_range()
    payload = build_ai_ops_queue(tenant, start_date, end_date, limit=limit, viewer=current_user)
    payload["enabled"] = True
    payload["model_policy"] = build_llm_task_policy("analytics")
    return _json_response(_with_access(payload, tenant))


@v2_analytics_bp.route("/operations/freshness", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_freshness_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    start_date, end_date = _date_range()
    payload = build_operational_freshness(
        tenant,
        start_date,
        end_date,
        viewer=current_user,
    )
    return _json_response(_with_access(payload, tenant))


@v2_analytics_bp.route("/operations/executive-summary", methods=["GET", "POST"])
@cutover_writer_view
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_executive_summary_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")

    start_date, end_date = _date_range()
    dashboard = _cached_operational_dashboard(tenant, start_date, end_date, viewer=current_user)
    summary = dashboard.get("summary") or {}
    has_data = any(
        int(summary.get(key) or 0) > 0
        for key in ("open_tickets", "overdue_tickets", "survey_responses", "chat_messages", "heatmap_points")
    )

    if has_data:
        analytics_report_input = {
            "tenant": dashboard.get("tenant"),
            "period": dashboard.get("period"),
            "scope": dashboard.get("scope") or {},
            "summary": summary,
            "trends": dashboard.get("trends"),
            "tickets": dashboard.get("tickets"),
            "maps": dashboard.get("maps"),
            "alerts": dashboard.get("alerts"),
            "next_best_actions": dashboard.get("next_best_actions"),
        }
        for section_name in ("surveys", "chats", "commerce", "employees"):
            section = dashboard.get(section_name) or {}
            if section.get("available") is not False:
                analytics_report_input[section_name] = section
        ai_report = generate_analytics_report(
            analytics_report_input,
            tenant_type=_tenant_analytics_segment(tenant),
        )
        reason_code = (
            "ai_scoped_summary_generated"
            if (dashboard.get("scope") or {}).get("category_scoped")
            else "ai_summary_generated"
        )
    else:
        ai_report = {
            "summary": "No hay datos suficientes para generar un resumen ejecutivo en el periodo seleccionado.",
            "opportunities": [],
            "threats": [],
            "tone": "Data-Insufficient",
        }
        reason_code = "no_operational_data_in_period"

    return _json_response(
        {
            "contract_version": "operations.executive_summary.v1",
            "tenant": dashboard.get("tenant"),
            "period": dashboard.get("period"),
            "generated_at": dashboard.get("generated_at"),
            "reason_code": reason_code,
            "scope": dashboard.get("scope") or {},
            "summary": summary,
            "ai": ai_report,
            "source_contract": dashboard.get("contract_version"),
            "model_policy": build_llm_task_policy("analytics"),
            "frontend_contract": {
                "render_as": "operations_ai_executive_summary",
                "dashboard_endpoint": "/api/v2/analytics/operations/dashboard",
                "export_pdf_endpoint": "/api/v2/analytics/operations/export.pdf",
                "refresh_behavior": "manual_or_after_dashboard_refresh",
            },
            "access": _integration_access(tenant),
        }
    )


@v2_analytics_bp.route("/operations/export.pdf", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operations_export_pdf_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    if not _feature_enabled(_integration_access(tenant), "analytics_dashboard"):
        return _integration_plan_required_response(tenant, "analytics_dashboard")

    request_id = _request_id()
    start_date, end_date = _date_range()
    dashboard = _cached_operational_dashboard(tenant, start_date, end_date, viewer=current_user)
    body = _build_simple_text_pdf(_dashboard_report_lines(dashboard))
    response = Response(
        body,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=operations_{tenant.slug or tenant.id}.pdf",
            "X-Request-Id": request_id,
        },
    )
    return response


@v2_analytics_bp.route("/summary", methods=["GET"])
def summary_alias_v2():
    return legacy_analytics.get_summary()


@v2_analytics_bp.route("/heatmap", methods=["GET"])
def heatmap_alias_v2():
    return legacy_analytics.get_heatmap()


@v2_analytics_bp.route("/surveys/summary", methods=["GET"])
def surveys_summary_alias_v2():
    return legacy_analytics.get_survey_summary()


@v2_analytics_bp.route("/surveys/sentiment", methods=["GET"])
def surveys_sentiment_alias_v2():
    return legacy_analytics.get_survey_sentiment()


@v2_analytics_bp.route("/surveys/geo", methods=["GET"])
def surveys_geo_alias_v2():
    return legacy_analytics.get_survey_geo()


@v2_analytics_bp.route("/insights", methods=["GET"])
def insights_alias_v2():
    return legacy_analytics.get_insights()


@v2_analytics_bp.route("/sales", methods=["GET"])
def sales_alias_v2():
    return legacy_analytics.get_sales_analytics()


@v2_analytics_bp.route("/benchmarks", methods=["GET"])
def benchmarks_alias_v2():
    return legacy_analytics.get_benchmarks()


@v2_analytics_bp.route("/report/latest", methods=["GET"])
def report_latest_alias_v2():
    return legacy_analytics.get_latest_report()


@v2_analytics_bp.route("/report/generate", methods=["POST"])
def report_generate_alias_v2():
    return legacy_analytics.trigger_generate_report()


@v2_analytics_bp.route("/generate-report", methods=["POST"])
def generate_report_alias_v2():
    return legacy_analytics.generate_report()
