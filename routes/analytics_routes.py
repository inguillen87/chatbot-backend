from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from services.analytics_service import analytics_service
from models import TenantProfile

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
