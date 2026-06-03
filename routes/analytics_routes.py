from functools import wraps

from flask import Blueprint, current_app, request, jsonify
from flask_login import current_user, login_user
from datetime import datetime, timedelta
from services.analytics_service import analytics_service
from services.openai_bridge import generate_analytics_report, analyze_sentiment
from services.plan_access import integration_access_payload
from extensions import db
from models import TenantProfile
from extensions import limiter
from utils.auth_helpers import obtener_token, user_from_token

analytics_v2_bp = Blueprint('analytics_v2_bp', __name__, url_prefix='/api/analytics')

def _request_id() -> str:
    return (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip() or f"req_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"


def _cors_origin() -> str | None:
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return None
    allowed = current_app.config.get("CORS_ALLOWED_ORIGINS") or current_app.config.get("ALLOWED_ORIGINS") or []
    if isinstance(allowed, str):
        allowed = [item.strip() for item in allowed.split(",") if item.strip()]
    normalized = origin.lower()
    if (
        origin in allowed
        or normalized.endswith(".chatboc.ar")
        or normalized == "https://www.chatboc.ar"
        or normalized.startswith("http://localhost")
        or normalized.startswith("http://127.0.0.1")
    ):
        return origin
    return None


@analytics_v2_bp.before_request
def _analytics_v2_options():
    if request.method == "OPTIONS":
        response = jsonify({"ok": True, "request_id": _request_id()})
        response.headers["X-Request-Id"] = _request_id()
        return response
    return None


@analytics_v2_bp.after_request
def _analytics_v2_cors(response):
    origin = _cors_origin()
    if origin:
        response.headers.setdefault("Access-Control-Allow-Origin", origin)
        response.headers.setdefault("Access-Control-Allow-Credentials", "true")
        response.headers.setdefault("Vary", "Origin")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Requested-With, X-Request-Id, X-Entity-Token, X-Tenant-Slug, X-Debug-Tenant",
        )
        response.headers.setdefault("Access-Control-Expose-Headers", "X-Request-Id")
    response.headers.setdefault("X-Request-Id", _request_id())
    return response


def _api_error(error: str, *, status: int, code: str) -> tuple:
    return jsonify({"error": error, "code": code, "request_id": _request_id()}), status


def api_login_required(fn):
    """API-safe auth guard that returns JSON 401 instead of HTML redirects."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not getattr(current_user, "is_authenticated", False):
            token = obtener_token()
            if token:
                user = user_from_token(token)
                if user is not None:
                    try:
                        login_user(user, remember=False, force=True)
                    except Exception:
                        current_app.logger.debug("[analytics_v2] token login fallback failed", exc_info=True)
        if not getattr(current_user, "is_authenticated", False):
            return _api_error("Unauthorized", status=401, code="auth_required")
        return fn(*args, **kwargs)

    return _wrapped


def _tenant_from_id(tenant_id) -> TenantProfile | None:
    try:
        return db.session.get(TenantProfile, int(tenant_id))
    except (TypeError, ValueError):
        return None


def _integration_access_for_tenant_id(tenant_id) -> dict:
    return integration_access_payload(_tenant_from_id(tenant_id))


def _feature_access(access: dict, feature_id: str) -> dict:
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


def _feature_enabled_for_tenant_id(tenant_id, feature_id: str) -> bool:
    access = _integration_access_for_tenant_id(tenant_id)
    return bool(_feature_access(access, feature_id).get("enabled"))


def _integration_plan_required_response(tenant_id, feature_id: str):
    access = _integration_access_for_tenant_id(tenant_id)
    feature = _feature_access(access, feature_id)
    return jsonify(
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
            "request_id": _request_id(),
        }
    ), 403


def _attach_access(payload, tenant_id):
    if isinstance(payload, dict):
        payload.setdefault("access", _integration_access_for_tenant_id(tenant_id))
    return payload


def _get_date_range():
    # Helper to parse dates
    # defaults to last 7 days
    now = datetime.utcnow()
    default_start = now - timedelta(days=7)

    from_str = request.args.get('from')
    to_str = request.args.get('to')

    start_date = default_start
    end_date = now

    if from_str:
        try:
            start_date = datetime.fromisoformat(from_str.replace('Z', '+00:00'))
        except ValueError:
            pass

    if to_str:
        try:
            end_date = datetime.fromisoformat(to_str.replace('Z', '+00:00'))
        except ValueError:
            pass

    return start_date, end_date

@analytics_v2_bp.route('/summary', methods=['GET'])
@api_login_required
def get_summary():
    # Verify tenant access
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    # Simple permission check
    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return _api_error("Unauthorized", status=403, code="forbidden")

    start_date, end_date = _get_date_range()
    context = request.args.get('context', 'overview')

    # Parse filters
    filters = {}
    channel = request.args.get('channel')
    if channel:
        filters['channel'] = channel

    try:
        data = analytics_service.get_summary(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date,
            context=context,
            filters=filters
        )
        return jsonify(_attach_access(data, tenant_id))
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/heatmap', methods=['GET'])
@api_login_required
def get_heatmap():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "heatmaps"):
        return _integration_plan_required_response(tenant_id, "heatmaps")

    start_date, end_date = _get_date_range()

    try:
        points = analytics_service.get_heatmap_data(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify({"points": points})
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/surveys/summary', methods=['GET'])
@api_login_required
def get_survey_summary():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")

    try:
        data = analytics_service.get_survey_summary(tenant_id=int(tenant_id))
        return jsonify(_attach_access(data, tenant_id))
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/surveys/sentiment', methods=['GET'])
@api_login_required
def get_survey_sentiment():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    tid = int(tenant_id)

    # 0. Check Cache (e.g. 24h)
    cached = analytics_service.get_cached_report(tid, "survey_sentiment", max_age_hours=24)
    if cached:
        return jsonify(cached)

    try:
        # 1. Fetch text data
        texts = analytics_service.get_survey_sentiment_texts(tenant_id=tid)

        # 2. Analyze with AI
        analysis = analyze_sentiment(texts)

        # 3. Cache Result
        analytics_service.cache_report(tid, "survey_sentiment", analysis)

        return jsonify(analysis)
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/surveys/geo', methods=['GET'])
@api_login_required
def get_survey_geo():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "heatmaps"):
        return _integration_plan_required_response(tenant_id, "heatmaps")

    try:
        points = analytics_service.get_survey_geo(tenant_id=int(tenant_id))
        return jsonify({"points": points})
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/insights', methods=['GET'])
@api_login_required
def get_insights():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    try:
        insights = analytics_service.get_insights(tenant_id=int(tenant_id))
        return jsonify({"insights": insights})
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/sales', methods=['GET'])
@api_login_required
def get_sales_analytics():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    start_date, end_date = _get_date_range()

    try:
        data = analytics_service.get_commerce_analytics(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/benchmarks', methods=['GET'])
@api_login_required
def get_benchmarks():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    start_date, end_date = _get_date_range()

    try:
        data = analytics_service.get_benchmarks(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/funnel', methods=['GET'])
@api_login_required
def get_funnel():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != "admin":
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    start_date, end_date = _get_date_range()

    try:
        data = analytics_service.get_funnel_analytics(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")

@analytics_v2_bp.route('/report/latest', methods=['GET'])
@api_login_required
def get_latest_report():
    """
    Returns the most recent valid cached report without triggering generation.
    """
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    segment = request.args.get('segment', 'pyme') # pyme or municipio

    cached = analytics_service.get_cached_report(int(tenant_id), f"consultant_{segment}", max_age_hours=24*7)

    if cached:
        cached['_cached'] = True
        cached.setdefault("contract_version", "analytics.report.latest.v1")
        cached.setdefault("request_id", _request_id())
        cached.setdefault("access", _integration_access_for_tenant_id(tenant_id))
        return jsonify(cached)

    return jsonify(
        {
            "contract_version": "analytics.report.latest.v1",
            "ok": True,
            "available": False,
            "report": None,
            "summary": None,
            "items": [],
            "reason_code": "report_not_generated",
            "message": "Todavia no hay un informe generado para este periodo.",
            "generate_endpoint": "/api/analytics/report/generate",
            "access": _integration_access_for_tenant_id(tenant_id),
            "request_id": _request_id(),
        }
    )

@analytics_v2_bp.route('/report/generate', methods=['POST'])
@api_login_required
@limiter.limit("1 per hour", key_func=lambda: str(current_user.id))
def trigger_generate_report():
    """
    Explicit endpoint to generate a report.
    Wraps the logic of `generate_report` but dedicated routing.
    Rate limited to 1 per hour per admin.
    """
    # Simply forward to the existing function logic
    return generate_report()

@analytics_v2_bp.route('/generate-report', methods=['POST'])
@api_login_required
@limiter.limit("1 per hour", key_func=lambda: str(current_user.id))
def generate_report():
    data = request.get_json(silent=True) or {}
    tenant_id = data.get('tenant_id')
    segment = data.get('segment', 'pyme') # pyme or municipio

    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
             return _api_error("Missing tenant_id", status=400, code="missing_tenant_id")

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return _api_error("Unauthorized", status=403, code="forbidden")
    if not _feature_enabled_for_tenant_id(tenant_id, "analytics_dashboard"):
        return _integration_plan_required_response(tenant_id, "analytics_dashboard")

    tid = int(tenant_id)

    # 0. Check Cache (e.g. 7 days for weekly reports)
    # The cache key should ideally include date range, but for simplicity we check if *any* report was generated recently
    # to prevent spamming.
    cached = analytics_service.get_cached_report(tid, f"consultant_{segment}", max_age_hours=24*7)
    force_refresh = data.get('force', False)

    if cached and not force_refresh:
        # Add metadata to indicate it's cached
        cached['_cached'] = True
        cached.setdefault("access", _integration_access_for_tenant_id(tenant_id))
        return jsonify(cached)

    from_str = data.get('from')
    to_str = data.get('to')

    now = datetime.utcnow()
    start_date = now - timedelta(days=7)
    end_date = now

    if from_str:
        try:
             start_date = datetime.fromisoformat(from_str.replace('Z', '+00:00'))
        except: pass
    if to_str:
        try:
             end_date = datetime.fromisoformat(to_str.replace('Z', '+00:00'))
        except: pass

    try:
        # 1. Aggregate stats
        summary = analytics_service.get_summary(
            tenant_id=tid,
            start_date=start_date,
            end_date=end_date,
            context=segment
        )

        # 2. Get extra stats based on segment
        if segment == 'pyme':
            commerce = analytics_service.get_commerce_analytics(
                tenant_id=tid,
                start_date=start_date,
                end_date=end_date
            )
            summary.update(commerce)
        elif segment == 'municipio':
            municipio_stats = analytics_service.get_municipio_analytics(
                tenant_id=tid,
                start_date=start_date,
                end_date=end_date
            )
            summary.update(municipio_stats)

        # 3. Call OpenAI
        report = generate_analytics_report(summary, tenant_type=segment)

        # 4. Cache Result
        analytics_service.cache_report(tid, f"consultant_{segment}", report)

        return jsonify(report)

    except Exception as e:
        current_app.logger.error(f"Analytics Error: {e}", exc_info=True)
        return _api_error(str(e), status=500, code="analytics_internal_error")
