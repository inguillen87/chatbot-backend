from flask import Blueprint, g, jsonify, request
from utils.auth_helpers import token_requerido
from services.analytics_kpis_service import analytics_kpi_service
from utils.auth_decorators import _is_authorized_for_tenant
from utils.roles import (
    ROLE_EMPLEADO,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    canonical_role,
    is_authorized_superadmin_user,
)

analytics_kpis_bp = Blueprint('analytics_kpis', __name__)
ANALYTICS_ROLES = {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_EMPLEADO}


def _resolve_actor_principal(current_user):
    return getattr(g, "current_user", None) or current_user


def _is_analytics_operator(current_user) -> bool:
    actor = _resolve_actor_principal(current_user)
    role = canonical_role(getattr(actor, "rol", None))
    if role not in ANALYTICS_ROLES:
        return False
    if role == ROLE_SUPERADMIN:
        return is_authorized_superadmin_user(actor)
    return True


def _can_access_tenant(current_user, tenant_id: int) -> bool:
    actor_principal = _resolve_actor_principal(current_user)
    return _is_authorized_for_tenant(actor_principal, tenant_id=tenant_id)


@analytics_kpis_bp.route('/api/analytics/kpis', methods=['GET'])
@token_requerido
def get_kpis(current_user):
    tenant_id = request.args.get('tenant_id', type=int)
    days = request.args.get('days', type=int, default=30)

    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'tenant_id is required'}}), 400

    if not _is_analytics_operator(current_user) or not _can_access_tenant(current_user, tenant_id):
        return jsonify({'error': {'code': 403, 'message': 'Forbidden access to this tenant'}}), 403

    metrics = analytics_kpi_service.get_operational_metrics(tenant_id, days)
    return jsonify(metrics), 200

@analytics_kpis_bp.route('/api/analytics/costs', methods=['GET'])
@token_requerido
def get_costs(current_user):
    tenant_id = request.args.get('tenant_id', type=int)

    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'tenant_id is required'}}), 400

    if not _is_analytics_operator(current_user) or not _can_access_tenant(current_user, tenant_id):
        return jsonify({'error': {'code': 403, 'message': 'Forbidden access to this tenant'}}), 403

    costs = analytics_kpi_service.get_cost_metrics(tenant_id)
    return jsonify(costs), 200
