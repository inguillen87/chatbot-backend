from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import uuid

from flask import Blueprint, jsonify, request

from models import TenantTicket
from routes import analytics_routes as legacy_analytics
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.analytics_service import analytics_service
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_analytics_bp = Blueprint("v2_analytics", __name__, url_prefix="/api/v2/analytics")


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
