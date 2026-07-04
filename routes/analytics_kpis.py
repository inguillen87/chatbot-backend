from flask import Blueprint, g, jsonify, request
from utils.auth_helpers import token_requerido
from services.analytics_kpis_service import analytics_kpi_service

analytics_kpis_bp = Blueprint('analytics_kpis', __name__)


def _resolve_actor_principal(current_user):
    return getattr(g, "owner_user", None) or getattr(g, "current_user", None) or current_user


def _is_admin_actor(current_user) -> bool:
    roles = {
        str(getattr(current_user, "rol", "") or "").strip().lower(),
        str(getattr(current_user, "role", "") or "").strip().lower(),
        str((getattr(g, "token_payload", {}) or {}).get("rol", "") or "").strip().lower(),
        str((getattr(g, "token_payload", {}) or {}).get("role", "") or "").strip().lower(),
    }
    return bool(roles.intersection({"admin", "superadmin", "super_admin", "owner"}))


@analytics_kpis_bp.route('/api/analytics/kpis', methods=['GET'])
@token_requerido
def get_kpis(current_user):
    actor_principal = _resolve_actor_principal(current_user)
    tenant_id = request.args.get('tenant_id', type=int)
    days = request.args.get('days', type=int, default=30)

    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'tenant_id is required'}}), 400

    # Ensure user has access to this tenant_id
    if not _is_admin_actor(current_user) and getattr(actor_principal, 'tenant_id', None) != tenant_id:
        # Fallback check for legacy models
        has_access = False
        if hasattr(actor_principal, 'municipio_id') and actor_principal.municipio_id == tenant_id:
            has_access = True
        elif hasattr(actor_principal, 'pyme_id') and actor_principal.pyme_id == tenant_id:
            has_access = True

        if not has_access:
            return jsonify({'error': {'code': 403, 'message': 'Forbidden access to this tenant'}}), 403

    metrics = analytics_kpi_service.get_operational_metrics(tenant_id, days)
    return jsonify(metrics), 200

@analytics_kpis_bp.route('/api/analytics/costs', methods=['GET'])
@token_requerido
def get_costs(current_user):
    actor_principal = _resolve_actor_principal(current_user)
    tenant_id = request.args.get('tenant_id', type=int)

    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'tenant_id is required'}}), 400

    # Ensure user has access to this tenant_id
    if not _is_admin_actor(current_user) and getattr(actor_principal, 'tenant_id', None) != tenant_id:
        has_access = False
        if hasattr(actor_principal, 'municipio_id') and actor_principal.municipio_id == tenant_id:
            has_access = True
        elif hasattr(actor_principal, 'pyme_id') and actor_principal.pyme_id == tenant_id:
            has_access = True

        if not has_access:
            return jsonify({'error': {'code': 403, 'message': 'Forbidden access to this tenant'}}), 403

    costs = analytics_kpi_service.get_cost_metrics(tenant_id)
    return jsonify(costs), 200
