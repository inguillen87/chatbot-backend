"""Institution knowledge management and a separately published read-only surface."""
import json
import os
from flask import Blueprint, request, jsonify, current_app
from models import TenantProfile
from extensions import limiter
from utils.auth_helpers import token_requerido, auth_sin_escrituras_implicitas
from utils.tenant_admin_access import can_manage_tenant_control_plane
from services.institutional_assistant import workspace, read_state, save_state, answer
from services.institutional_assistant_content import ContentError, MAX_BYTES
institutional_assistant_bp = Blueprint('institutional_assistant', __name__)

def _reply(data, status=200): return jsonify(data), status

@institutional_assistant_bp.after_request
def _private(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie, Authorization, Origin'
    return response

def _tenant(slug, actor=None):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if tenant is None or not tenant.is_active: raise ContentError('knowledge_not_available', 404)
    if actor is not None and not can_manage_tenant_control_plane(actor, tenant):
        raise ContentError('knowledge_forbidden', 403)
    expected = {'tenant': slug, 'tenant_slug': slug, 'tenant_id': str(tenant.id)}
    for key in request.args:
        if key not in expected or request.args.getlist(key) != [expected[key]]:
            raise ContentError('knowledge_scope_mismatch', 400)
    for header, target in [('X-Tenant', slug), ('X-Tenant-Slug', slug), ('X-Tenant-ID', str(tenant.id))]:
        if header in request.headers and request.headers[header] != target:
            raise ContentError('knowledge_scope_mismatch', 400)
    return tenant

def _origin():
    allowed = {s.strip() for s in os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',') if s.strip() and s.strip() != '*'}
    allowed.add(request.host_url.rstrip('/'))
    if request.headers.get('Origin') is not None and request.headers['Origin'] not in allowed:
        raise ContentError('knowledge_origin_forbidden', 403)

def _json():
    if not request.is_json or not request.content_length or request.content_length > MAX_BYTES + 8192:
        raise ContentError('knowledge_json_required', 413)
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result: raise ContentError('knowledge_duplicate_key', 400)
            result[k] = v
        return result
    try:
        value = json.loads(request.get_data(), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise ContentError('knowledge_json_invalid', 400) from error
    if not isinstance(value, dict): raise ContentError('knowledge_command_invalid', 400)
    return value

@institutional_assistant_bp.errorhandler(ContentError)
def _error(error): return _reply({'reason_code': error.code}, error.status)

@institutional_assistant_bp.route('/api/admin/tenants/<slug>/institutional-assistant', methods=['GET', 'PUT'])
@token_requerido
@auth_sin_escrituras_implicitas
def manage(actor, slug):
    _origin()
    tenant = _tenant(slug, actor)
    if request.method == 'GET': return _reply(workspace(tenant, read_state(tenant), editable=True))
    from cutover_writer_fence import cutover_writer_fence_enabled
    if cutover_writer_fence_enabled(current_app.config): raise ContentError('knowledge_maintenance', 503)
    if request.headers.get('X-Chatboc-Knowledge') != '1': raise ContentError('knowledge_confirmation_required', 400)
    return _reply(save_state(tenant, actor, _json()))

@institutional_assistant_bp.route('/api/admin/tenants/<slug>/institutional-assistant/answer', methods=['POST'])
@limiter.limit('30 per minute')
@token_requerido
@auth_sin_escrituras_implicitas
def private_answer(actor, slug):
    _origin()
    return _reply(answer(_tenant(slug, actor), _json()))

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant', methods=['GET'])
def public_workspace(slug):
    tenant = _tenant(slug)
    return _reply(workspace(tenant, read_state(tenant, public=True)))

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/answer', methods=['POST'])
@limiter.limit('20 per minute')
def public_answer(slug):
    _origin()
    return _reply(answer(_tenant(slug), _json(), public=True))
