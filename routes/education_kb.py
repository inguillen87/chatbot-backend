from flask import Blueprint, jsonify, request

from models import TenantProfile
from services.education_kb_service import education_kb_service
from utils.auth_helpers import token_requerido

education_kb_bp = Blueprint('education_kb', __name__)


def _resolve_actor_tenant_id(current_user=None, actor_principal=None):
    actor = actor_principal or current_user
    tenant_id = getattr(actor, 'tenant_id', None)
    if tenant_id:
        return tenant_id

    municipio_owner_id = getattr(actor, 'municipio_id', None)
    if municipio_owner_id:
        tenant = TenantProfile.query.filter_by(municipio_id=municipio_owner_id).first()
        if tenant:
            return tenant.id

    actor_user_id = getattr(actor, 'id', None)
    if actor_user_id:
        tenant = TenantProfile.query.filter((TenantProfile.pyme_id == actor_user_id) | (TenantProfile.municipio_id == actor_user_id)).first()
        if tenant:
            return tenant.id

    pyme_owner_id = getattr(actor, 'pyme_id', None)
    if pyme_owner_id:
        tenant = TenantProfile.query.filter_by(pyme_id=pyme_owner_id).first()
        if tenant:
            return tenant.id

    return None


@education_kb_bp.route('/api/v1/education/knowledge/query', methods=['POST'])
@token_requerido
def query_knowledge(current_user, actor_principal=None):
    data = request.json or {}
    query = data.get('query')

    if not actor_principal:
        actor_principal = current_user

    if not query:
        return jsonify({'error': {'code': 400, 'message': 'query is required'}}), 400

    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'Tenant context required'}}), 400

    school_id = data.get('school_id')

    try:
        results = education_kb_service.query_knowledge_base(tenant_id, query, school_id=school_id)
        return jsonify(results), 200
    except Exception as e:
        return jsonify({'error': {'code': 500, 'message': str(e)}}), 500
