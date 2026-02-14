"""Analytics blueprint exposing CRM dashboards and data endpoints."""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from extensions import db

from services.analytics.cache import analytics_cache
from services.analytics.config import get_config
from services.analytics.filters import AnalyticsFilters, parse_filters
from services.analytics import (
    get_breakdown,
    get_cohorts,
    get_geo_heatmap,
    get_geo_points,
    get_operations_overview,
    get_summary,
    get_timeseries,
    get_top,
    get_whatsapp_templates,
)
from services.analytics.ingestor import analytics_ingestor
from services.analytics.models import AnalyticsModuleStatus
from services.analytics.rbac import require_access

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analytics")


@analytics_bp.before_request
def _ensure_feature_enabled() -> None:
    config = get_config()
    if not config.feature_enabled:
        abort(404)


def _json_response(payload, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    return response


@analytics_bp.route("/summary", methods=["GET"])
def analytics_summary():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    current_app.logger.info("[analytics] summary %s", filters)
    data = get_summary(filters)
    return _json_response(data)


@analytics_bp.route("/timeseries", methods=["GET"])
def analytics_timeseries():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    metric = request.args.get("metric", "tickets")
    group = request.args.get("group")
    data = get_timeseries(filters, metric=metric, group=group)
    return _json_response(data)


@analytics_bp.route("/breakdown", methods=["GET"])
def analytics_breakdown():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    dimension = request.args.get("dimension", "categoria")
    data = get_breakdown(filters, dimension=dimension)
    return _json_response(data)


@analytics_bp.route("/geo/heatmap", methods=["GET"])
def analytics_heatmap():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    data = get_geo_heatmap(filters)
    return _json_response(data)


@analytics_bp.route("/geo/points", methods=["GET"])
def analytics_points():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    limit = int(request.args.get("limit", 500))
    data = get_geo_points(filters, limit=limit)
    return _json_response(data)


@analytics_bp.route("/top", methods=["GET"])
def analytics_top():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    category = request.args.get("category", "barrios")
    limit = int(request.args.get("limit", 10))
    data = get_top(filters, category=category, limit=limit)
    return _json_response(data)


@analytics_bp.route("/operations", methods=["GET"])
def analytics_operations():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    data = get_operations_overview(filters)
    return _json_response(data)


@analytics_bp.route("/cohorts", methods=["GET"])
def analytics_cohorts():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    data = get_cohorts(filters)
    return _json_response(data)


@analytics_bp.route("/whatsapp/templates", methods=["GET"])
def analytics_templates():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    data = get_whatsapp_templates(filters)
    return _json_response(data)


@analytics_bp.route("/health", methods=["GET"])
def analytics_health():
    latest = (
        db.session.query(AnalyticsModuleStatus)
        .order_by(AnalyticsModuleStatus.snapshot_at.desc())
        .first()
    )
    payload = {
        "cache": {
            "hits": analytics_cache.stats.hits,
            "misses": analytics_cache.stats.misses,
            "evictions": analytics_cache.stats.evictions,
        },
        "jobs": {
            "pending": latest.jobs_pending if latest else 0,
            "running": latest.jobs_running if latest else 0,
            "failed": latest.jobs_failed if latest else 0,
        },
        "last_snapshot": latest.snapshot_at.isoformat() if latest else None,
        "metadata": latest.metadata if latest else {},
    }
    return _json_response(payload)


@analytics_bp.route("/event", methods=["POST"])
def analytics_event_ingest():
    payload = request.get_json(silent=True) or {}
    tenant_raw = payload.get("tenant_id")
    if tenant_raw is None:
        abort(400, description="tenant_id is required")

    try:
        tenant_id = int(tenant_raw)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be an integer")

    event_name = (payload.get("event_name") or "").strip()
    if not event_name:
        abort(400, description="event_name is required")

    require_access(str(tenant_id), "operador")
    analytics_ingestor.track(
        tenant_id=tenant_id,
        event_name=event_name,
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
        user_id=payload.get("user_id"),
        anon_id=payload.get("anon_id"),
        channel=payload.get("channel"),
        session_id=payload.get("session_id"),
        lat=payload.get("lat"),
        lng=payload.get("lng"),
        entity_ref=payload.get("entity_ref"),
        tenant_type=payload.get("tenant_type"),
    )
    return _json_response({"ok": True}, status=202)


@analytics_bp.route("/ui", methods=["GET"])
def analytics_ui():
    tenant_id = request.args.get("tenant_id")
    scope = request.args.get("scope", "municipio")
    return render_template("analytics/dashboard.html", tenant_id=tenant_id, scope=scope)


@analytics_bp.route("", methods=["GET"])
def analytics_root():
    return analytics_ui()
