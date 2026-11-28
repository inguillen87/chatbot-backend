"""Institution knowledge management and a separately published read-only surface."""
import json
import os
import re
from flask import Blueprint, request, jsonify, current_app, Response
from models import TenantProfile
from extensions import limiter
from utils.auth_helpers import token_requerido, auth_sin_escrituras_implicitas
from utils.tenant_admin_access import can_manage_tenant_control_plane
from services.institutional_assistant import workspace, read_state, save_state, answer
from services.institutional_assistant_content import ContentError, MAX_BYTES
from services.institutional_assistant_metrics import (
    DEFAULT_DAYS, MAX_DAYS, MAX_UTC_OFFSET_MINUTES, observe_question_outcomes,
    question_metrics, record_answer_feedback, record_question_outcomes,
)
institutional_assistant_bp = Blueprint('institutional_assistant', __name__)

def _reply(data, status=200): return jsonify(data), status

@institutional_assistant_bp.after_request
def _private(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie, Authorization, Origin'
    return response

def _tenant(slug, actor=None, *, canonical_revision=None, optional=()):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if tenant is None or not tenant.is_active: raise ContentError('knowledge_not_available', 404)
    if actor is not None and not can_manage_tenant_control_plane(actor, tenant):
        raise ContentError('knowledge_forbidden', 403)
    expected = {'tenant': slug, 'tenant_slug': slug, 'tenant_id': str(tenant.id)}
    if canonical_revision is not None:
        expected['revision'] = canonical_revision
    for key in request.args:
        if key in optional:
            continue
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

def _canonical_revision():
    revisions = request.args.getlist('revision')
    if (request.content_length or len(revisions) != 1
        or not re.fullmatch(r'[a-f0-9]{64}', revisions[0])):
        raise ContentError('knowledge_command_invalid', 400)
    return revisions[0]

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
    return _reply(answer(_tenant(slug, actor), _json(), actor=actor))

@institutional_assistant_bp.route('/api/admin/tenants/<slug>/institutional-assistant/nodes/<node_id>', methods=['GET'])
@limiter.limit('30 per minute')
@token_requerido
@auth_sin_escrituras_implicitas
def private_canonical_node(actor, slug, node_id):
    _origin()
    revision = _canonical_revision()
    tenant = _tenant(slug, actor, canonical_revision=revision)
    return _reply(answer(tenant, {'revision': revision, 'node_id': node_id}, actor=actor))

def _bounded_integer(name, default, minimum, maximum):
    values = request.args.getlist(name)
    if not values:
        return default
    if len(values) != 1 or not re.fullmatch(r'-?[0-9]{1,4}', values[0]) or not minimum <= int(values[0]) <= maximum:
        raise ContentError('knowledge_command_invalid', 400)
    return int(values[0])

@institutional_assistant_bp.route('/api/admin/tenants/<slug>/institutional-assistant/metrics', methods=['GET'])
@limiter.limit('30 per minute')
@token_requerido
@auth_sin_escrituras_implicitas
def private_question_metrics(actor, slug):
    _origin()
    if request.content_length:
        raise ContentError('knowledge_command_invalid', 400)
    tenant = _tenant(slug, actor, optional=('days', 'utc_offset_minutes'))
    days = _bounded_integer('days', DEFAULT_DAYS, 1, MAX_DAYS)
    offset = _bounded_integer('utc_offset_minutes', 0, -MAX_UTC_OFFSET_MINUTES, MAX_UTC_OFFSET_MINUTES)
    return _reply(question_metrics(tenant, read_state(tenant), days=days, utc_offset_minutes=offset))

@institutional_assistant_bp.route('/api/admin/tenants/<slug>/institutional-assistant/whatsapp-flow', methods=['GET'])
@limiter.limit('30 per minute')
@token_requerido
@auth_sin_escrituras_implicitas
def private_whatsapp_flow(actor, slug):
    """The «Encontrá tu trámite» WhatsApp Flow built from the published pack."""
    _origin()
    if request.content_length:
        raise ContentError('knowledge_command_invalid', 400)
    from services.institutional_consult_flow import build_consult_flow, consult_flow_summary, consult_options

    tenant = _tenant(slug, actor)
    published = workspace(tenant, read_state(tenant, public=True), public=True)
    knowledge = published.get('knowledge') if isinstance(published, dict) else None
    if not isinstance(knowledge, dict):
        raise ContentError('knowledge_not_published', 409)
    organization = str((published.get('tenant') or {}).get('name') or getattr(tenant, 'nombre', '') or tenant.slug)
    try:
        artifact = build_consult_flow(organization=organization, knowledge=knowledge)
    except ValueError:
        raise ContentError('knowledge_pack_without_topics', 409)
    import json as _json_module
    topics, localities = consult_options(knowledge)
    return _reply({
        'contract_version': 'chatboc.institutional_whatsapp_flow.v1',
        'revision': published.get('revision'),
        'name': f'{organization[:40]} - Encontrá tu trámite',
        'flow_json': _json_module.loads(artifact.as_bytes()),
        'summary': consult_flow_summary(topics, localities),
    })

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant', methods=['GET'])
def public_workspace(slug):
    tenant = _tenant(slug)
    return _reply(workspace(tenant, read_state(tenant, public=True), public=True))

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/answer', methods=['POST'])
@limiter.limit('20 per minute')
def public_answer(slug):
    # Same non-credentialed CORS surface as the public workspace. No session or
    # caller identity is used; only explicitly published tenant content is read.
    tenant, command = _tenant(slug), _json()
    tenant_id, tenant_type = tenant.id, tenant.tipo
    with observe_question_outcomes() as outcomes:
        try:
            result = answer(tenant, command, public=True)
        finally:
            # Anonymous count of the typed-question outcome (canonical node ids
            # only). Best effort: it never changes the response or its status.
            record_question_outcomes(tenant_id, tenant_type, 'web_reader', outcomes)
    return _reply(result)

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/feedback', methods=['POST'])
@limiter.limit('20 per minute')
def public_answer_feedback(slug):
    # «¿Te sirvió esta respuesta?»: a yes/no about one published topic. No
    # comment field exists, and no session or caller identity is read.
    tenant, command = _tenant(slug), _json()
    state = read_state(tenant, public=True)
    if (set(command) != {'revision', 'node_id', 'helpful'} or command['revision'] != state['revision']
            or not isinstance(command['node_id'], str) or command['node_id'] not in state['bundle']['nodes']
            or type(command['helpful']) is not bool):
        raise ContentError('knowledge_command_invalid', 400)
    record_answer_feedback(tenant.id, tenant.tipo, 'web_reader', state['revision'], command['node_id'], command['helpful'])
    return _reply({'received': True})

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/nodes/<node_id>', methods=['GET'])
@limiter.limit('20 per minute')
def public_canonical_node(slug, node_id):
    revision = _canonical_revision()
    tenant = _tenant(slug, canonical_revision=revision)
    return _reply(answer(tenant, {'revision': revision, 'node_id': node_id}, public=True))

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/audio', methods=['POST'])
def public_audio(slug):
    from services.institutional_assistant_audio import MAX_AUDIO_COMMAND_BYTES, synthesize_public_nodes
    if not request.content_length or request.content_length > MAX_AUDIO_COMMAND_BYTES:
        raise ContentError('knowledge_command_invalid', 400)
    tenant = _tenant(slug)
    audio, revision = synthesize_public_nodes(tenant, _json())
    response = Response(audio, content_type='audio/mpeg')
    response.headers['Content-Length'] = str(len(audio))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Chatboc-Knowledge-Revision'] = revision
    response.headers['X-Chatboc-Tenant-ID'] = str(tenant.id)
    return response

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/easy-read', methods=['POST'])
@limiter.limit('20 per minute')
def public_easy_read(slug):
    from services.institutional_assistant_easy_read import MAX_EASY_READ_COMMAND_BYTES, easy_read_public_node
    if not request.content_length or request.content_length > MAX_EASY_READ_COMMAND_BYTES:
        raise ContentError('knowledge_command_invalid', 400)
    tenant = _tenant(slug)
    return _reply(easy_read_public_node(tenant, _json()))

def _source_reply(tenant, source_id, revision, *, public=False, actor=None):
    from services.institutional_assistant_sources import source_document
    data, filename, mime = source_document(tenant, source_id, revision, public=public, actor=actor)
    # The bounded buffer is completely verified before the first response byte.
    response = Response((data[offset:offset + 65536] for offset in range(0, len(data), 65536)),
                        content_type='text/plain; charset=utf-8' if mime == 'text/plain' else mime)
    response.headers['Content-Length'] = str(len(data))
    disposition = 'attachment' if mime == 'text/plain' else 'inline'
    response.headers['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@institutional_assistant_bp.route('/api/admin/tenants/<slug>/institutional-assistant/sources/<source_id>', methods=['GET'])
@limiter.limit('30 per minute')
@token_requerido
@auth_sin_escrituras_implicitas
def private_source_pdf(actor, slug, source_id):
    _origin()
    revision = _canonical_revision()
    tenant = _tenant(slug, actor, canonical_revision=revision)
    return _source_reply(tenant, source_id, revision, actor=actor)

@institutional_assistant_bp.route('/api/public/tenants/<slug>/institutional-assistant/sources/<source_id>', methods=['GET'])
@limiter.limit('20 per minute')
def public_source_pdf(slug, source_id):
    revision = _canonical_revision()
    tenant = _tenant(slug, canonical_revision=revision)
    return _source_reply(tenant, source_id, revision, public=True)
