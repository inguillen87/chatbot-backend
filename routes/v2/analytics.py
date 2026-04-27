from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from models import TenantTicket
from routes import analytics_routes as legacy_analytics
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.analytics_service import analytics_service
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_analytics_bp = Blueprint("v2_analytics", __name__, url_prefix="/api/v2/analytics")


def _resolve_tenant_or_error(current_user):
    explicit_slug = (request.headers.get("X-Tenant-Slug") or request.args.get("tenant_slug") or "").strip()
    if not explicit_slug:
        return None, (jsonify({"error": "X-Tenant-Slug es obligatorio en analytics v2"}), 400)

    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=explicit_slug)
    except V2TenantResolutionError as exc:
        return None, (jsonify({"error": exc.message}), exc.status_code)

    role = str(getattr(current_user, "rol", "") or "").lower()
    if role != "super_admin":
        same_slug = (getattr(current_user, "tenant_slug", "") or "").strip().lower() == (tenant.slug or "").strip().lower()
        same_id = str(getattr(current_user, "tenant_id", "") or "") == str(tenant.id)
        if not same_slug and not same_id:
            return None, (jsonify({"error": "Permisos insuficientes para este tenant"}), 403)

    return tenant, None


def _date_range():
    now = datetime.utcnow()
    default_start = now - timedelta(days=7)

    from_str = request.args.get("from")
    to_str = request.args.get("to")

    start_date = default_start
    end_date = now

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


@v2_analytics_bp.route("/overview", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
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

    return jsonify(
        {
            "tenant_id": tenant.id,
            "total_conversations": int((summary.get("kpis") or {}).get("total_interactions") or 0),
            "total_tickets": int(TenantTicket.query.filter_by(tenant_id=tenant.id).count()),
            "open_tickets": int(
                TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id, TenantTicket.estado.notin_(["cerrado", "closed"]))
                .count()
            ),
            "overdue_tickets": int(
                TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id, TenantTicket.estado.in_(["vencido", "overdue"])).count()
            ),
            "avg_first_response_time": None,
            "avg_resolution_time": None,
            "survey_response_count": int(((analytics_service.get_survey_summary(tenant.id) or {}).get("stats") or {}).get("total_votes") or 0),
            "survey_completion_rate": float(((analytics_service.get_survey_summary(tenant.id) or {}).get("stats") or {}).get("participation_rate") or 0.0),
            "csat_score": None,
            "nps_score": None,
        }
    )


@v2_analytics_bp.route("/tickets", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
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
    return jsonify(data)


@v2_analytics_bp.route("/surveys", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def surveys_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    data = analytics_service.get_survey_summary(tenant.id)
    return jsonify(data)


@v2_analytics_bp.route("/funnel", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def funnel_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    start_date, end_date = _date_range()
    data = analytics_service.get_funnel_analytics(tenant.id, start_date, end_date)
    return jsonify(data)


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
