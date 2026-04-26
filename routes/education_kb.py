from flask import Blueprint, jsonify, request
from utils.auth_helpers import token_requerido
from services.education_kb_service import education_kb_service

education_kb_bp = Blueprint('education_kb', __name__)

@education_kb_bp.route('/api/v1/education/knowledge/query', methods=['POST'])
@token_requerido
def query_knowledge(current_user, actor_principal=None):
    data = request.json or {}
    query = data.get("query")

    if not actor_principal:
        actor_principal = current_user

    if not query:
        return jsonify({'error': {'code': 400, 'message': 'query is required'}}), 400

    tenant_id = getattr(actor_principal, 'tenant_id', None) or getattr(actor_principal, 'municipio_id', None) or getattr(actor_principal, 'pyme_id', None)
    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'Tenant context required'}}), 400

    try:
        results = education_kb_service.query_knowledge_base(tenant_id, query)
        return jsonify(results), 200
    except Exception as e:
        return jsonify({'error': {'code': 500, 'message': str(e)}}), 500
