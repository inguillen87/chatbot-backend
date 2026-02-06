from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from services.analytics_service import analytics_service
from services.openai_bridge import generate_analytics_report, analyze_sentiment
from models import TenantProfile
from extensions import limiter

analytics_v2_bp = Blueprint('analytics_v2_bp', __name__, url_prefix='/api/analytics')

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
@login_required
def get_summary():
    # Verify tenant access
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    # Simple permission check
    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

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
        return jsonify(data)
    except Exception as e:
        print(f"Analytics Error: {e}")
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/heatmap', methods=['GET'])
@login_required
def get_heatmap():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    start_date, end_date = _get_date_range()

    try:
        points = analytics_service.get_heatmap_data(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify({"points": points})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/surveys/summary', methods=['GET'])
@login_required
def get_survey_summary():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    try:
        data = analytics_service.get_survey_summary(tenant_id=int(tenant_id))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/surveys/sentiment', methods=['GET'])
@login_required
def get_survey_sentiment():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

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
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/surveys/geo', methods=['GET'])
@login_required
def get_survey_geo():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    try:
        points = analytics_service.get_survey_geo(tenant_id=int(tenant_id))
        return jsonify({"points": points})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/insights', methods=['GET'])
@login_required
def get_insights():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    try:
        insights = analytics_service.get_insights(tenant_id=int(tenant_id))
        return jsonify({"insights": insights})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/sales', methods=['GET'])
@login_required
def get_sales_analytics():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    start_date, end_date = _get_date_range()

    try:
        data = analytics_service.get_commerce_analytics(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/benchmarks', methods=['GET'])
@login_required
def get_benchmarks():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    start_date, end_date = _get_date_range()

    try:
        data = analytics_service.get_benchmarks(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/funnel', methods=['GET'])
@login_required
def get_funnel():
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    start_date, end_date = _get_date_range()

    try:
        data = analytics_service.get_funnel_analytics(
            tenant_id=int(tenant_id),
            start_date=start_date,
            end_date=end_date
        )
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@analytics_v2_bp.route('/report/latest', methods=['GET'])
@login_required
def get_latest_report():
    """
    Returns the most recent valid cached report without triggering generation.
    """
    tenant_id = request.args.get('tenant_id')
    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
            return jsonify({"error": "Missing tenant_id"}), 400

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    segment = request.args.get('segment', 'pyme') # pyme or municipio

    cached = analytics_service.get_cached_report(int(tenant_id), f"consultant_{segment}", max_age_hours=24*7)

    if cached:
        cached['_cached'] = True
        return jsonify(cached)

    return jsonify({"error": "No cached report found", "code": 404}), 404

@analytics_v2_bp.route('/report/generate', methods=['POST'])
@login_required
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
@login_required
@limiter.limit("1 per hour", key_func=lambda: str(current_user.id))
def generate_report():
    data = request.get_json()
    tenant_id = data.get('tenant_id')
    segment = data.get('segment', 'pyme') # pyme or municipio

    if not tenant_id:
        if current_user.tenant_id:
            tenant_id = current_user.tenant_id
        else:
             return jsonify({"error": "Missing tenant_id"}), 400

    if current_user.tenant_id and str(current_user.tenant_id) != str(tenant_id) and current_user.rol != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    tid = int(tenant_id)

    # 0. Check Cache (e.g. 7 days for weekly reports)
    # The cache key should ideally include date range, but for simplicity we check if *any* report was generated recently
    # to prevent spamming.
    cached = analytics_service.get_cached_report(tid, f"consultant_{segment}", max_age_hours=24*7)
    force_refresh = data.get('force', False)

    if cached and not force_refresh:
        # Add metadata to indicate it's cached
        cached['_cached'] = True
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

        # 2. Get extra stats if PyME
        if segment == 'pyme':
            commerce = analytics_service.get_commerce_analytics(
                tenant_id=tid,
                start_date=start_date,
                end_date=end_date
            )
            summary.update(commerce)

        # 3. Call OpenAI
        report = generate_analytics_report(summary, tenant_type=segment)

        # 4. Cache Result
        analytics_service.cache_report(tid, f"consultant_{segment}", report)

        return jsonify(report)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
