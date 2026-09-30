"""Persisted institution knowledge used by the app and the existing chat handler."""
from copy import deepcopy
from sqlalchemy import select
from flask import current_app
from models import db, TenantProfile, TenantConfig, AuditEvent, User
from cutover_writer_fence import cutover_writer_fence_enabled
from services.constants import CONTEXTO_MUNICIPIO
from utils.tenant_admin_access import can_manage_tenant_control_plane
from services.institutional_assistant_content import (
    ContentError, CONTRACT, normalize_bundle, overview, digest, select_nodes, materialize_node,
)
KEY, CHANNEL = 'institutional_assistant', 'knowledge'
UI = {
    'heading': 'Información y orientación', 'description': 'Consultá por tema o escribí tu pregunta.',
    'topics': 'Temas de consulta', 'sources': 'Documentos y fuentes', 'source_details': 'Consultar fuentes',
    'question': 'Escribí tu consulta', 'placeholder': '¿Sobre qué necesitás información?',
    'send': 'Consultar', 'back': 'Volver', 'home': 'Todos los temas', 'loading': 'Consultando la información…',
    'unknown': 'Esta información no está incluida en las fuentes disponibles. Podés elegir otro tema.',
    'error': 'No se pudo completar la consulta. Volvé a intentarlo o elegí un tema.',
    'retry': 'Volver a consultar', 'import': 'Incorporar conocimiento',
    'import_help': 'Cargá la versión integrada de documentos, respuestas y menús de esta organización.',
    'empty': 'Todavía no se incorporó una versión de conocimiento en este espacio.',
    'choose_file': 'Seleccionar versión', 'private': 'Uso interno', 'public': 'Disponible para consultas',
    'publish': 'Habilitar en el agente', 'retire': 'Retirar del agente', 'confirm': 'Confirmar cambio',
    'cancel': 'Cancelar', 'confirm_publish': 'Las respuestas de esta versión quedarán disponibles en el agente de esta organización.',
    'confirm_retire': 'El agente dejará de utilizar esta versión para nuevas consultas.',
    'version': 'Versión', 'preview': 'Probar respuestas', 'large_text': 'Texto ampliado',
    'answer': 'Respuesta', 'evidence': 'Información respaldada por documentos', 'close': 'Cerrar fuentes',
    'pending': 'El cambio no está confirmado. Consultá el estado antes de volver a intentarlo.',
}

def _record(tenant_id):
    return TenantConfig.query.filter_by(tenant_id=tenant_id, key=KEY, channel=CHANNEL).first()

def read_state(tenant, *, public=False):
    if getattr(tenant, 'is_active', False) is not True:
        raise ContentError('knowledge_not_available', 404)
    row = _record(tenant.id)
    state = deepcopy(row.json_value) if row else None
    if state is not None:
        if not isinstance(state, dict) or not isinstance(state.get('bundle'), dict):
            raise ContentError('knowledge_state_invalid', 503)
        bundle = state['bundle']
        if (bundle.get('tenant') != {'id': tenant.id, 'slug': tenant.slug}
            or digest(bundle) != state.get('bundle_hash') or type(state.get('generation')) is not int
            or state['generation'] < 1 or state.get('visibility') not in ('private', 'public')
            or state.get('revision') != digest({key: state.get(key) for key in ('bundle_hash','generation','visibility')})):
            raise ContentError('knowledge_state_invalid', 503)
    if public and (state is None or state.get('visibility') != 'public'):
        raise ContentError('knowledge_not_available', 404)
    return state

def workspace(tenant, state, *, editable=False):
    data = {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug, 'name': tenant.nombre},
        'revision': state['revision'] if state else None, 'visibility': state['visibility'] if state else 'empty',
        'can_edit': editable, 'ui': deepcopy(UI), 'knowledge': None}
    if state:
        bundle = state['bundle']
        data['knowledge'] = {**overview(bundle), 'version': bundle['version'], 'initial': materialize_node(bundle['nodes'][bundle['start']])}
    return data

def save_state(tenant, actor, command):
    if not isinstance(command, dict) or set(command) - {'operation', 'expected_revision', 'bundle'}:
        raise ContentError('knowledge_command_invalid', 400)
    if 'expected_revision' not in command or command.get('operation') not in ('import', 'publish', 'retire'):
        raise ContentError('knowledge_command_invalid', 400)
    operation = command['operation']
    try:
        current = db.session.execute(select(TenantProfile).where(TenantProfile.id == tenant.id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one()
        fresh_actor = db.session.get(User, actor.id, populate_existing=True)
        if not current.is_active or not can_manage_tenant_control_plane(fresh_actor, current):
            raise ContentError('knowledge_forbidden', 403)
        row = _record(current.id)
        if row: db.session.refresh(row)
        previous = read_state(current)
        if command['expected_revision'] != (previous['revision'] if previous else None):
            raise ContentError('knowledge_revision_conflict', 412)
        if operation == 'import':
            bundle = normalize_bundle(command.get('bundle'), current.id, current.slug)
            visibility = 'private'
        else:
            if previous is None or 'bundle' in command:
                raise ContentError('knowledge_command_invalid', 400)
            bundle = previous['bundle']
            visibility = 'public' if operation == 'publish' else 'private'
        generation = (previous['generation'] if previous else 0) + 1
        state = {'bundle': bundle, 'bundle_hash': digest(bundle), 'generation': generation, 'visibility': visibility}
        state['revision'] = digest({'bundle_hash': state['bundle_hash'], 'generation': generation, 'visibility': visibility})
        if row is None:
            row = TenantConfig(tenant_id=current.id, key=KEY, channel=CHANNEL, json_value=state)
            db.session.add(row)
        else:
            row.json_value = state
        db.session.add(AuditEvent(event_type='institutional_knowledge.' + operation, tenant_id=current.id,
            actor_user_id=fresh_actor.id, details={'revision': state['revision'], 'generation': generation,
            'node_count': len(bundle['nodes']), 'source_hashes': [s['sha256'] for s in bundle['sources'].values()]}))
        if cutover_writer_fence_enabled(current_app.config):
            raise ContentError('knowledge_maintenance', 503)
        db.session.commit()
        return workspace(current, read_state(current), editable=True)
    except ContentError:
        db.session.rollback()
        raise
    except Exception as error:
        db.session.rollback()
        raise ContentError('knowledge_write_unconfirmed', 503) from error

def answer(tenant, command, *, public=False, selector=None, actor=None):
    if not isinstance(command, dict) or set(command) - {'revision', 'node_id', 'question'}:
        raise ContentError('knowledge_command_invalid', 400)
    state = read_state(tenant, public=public)
    if state is None or command.get('revision') != state['revision']:
        raise ContentError('knowledge_revision_conflict', 412)
    from utils.auth_helpers import auth_session_version
    actor_id = getattr(actor, 'id', None)
    session_version = auth_session_version(actor) if actor_id else None
    bundle = state['bundle']
    node_id = command.get('node_id', bundle['start'])
    if not isinstance(node_id, str) or node_id not in bundle['nodes']:
        raise ContentError('knowledge_node_not_found', 400)
    question = command.get('question')
    called = question is not None
    if called:
        if selector is None:
            from services.llm_utils import llamar_llm_para_json_estructurado
            selector = llamar_llm_para_json_estructurado
        nodes = select_nodes(bundle, question, node_id, selector)
    else:
        nodes = [deepcopy(bundle['nodes'][node_id])]
    db.session.expire_all()
    latest = read_state(tenant, public=public)
    if actor_id:
        refreshed_actor = db.session.get(User, actor_id, populate_existing=True)
        if refreshed_actor is None or not can_manage_tenant_control_plane(refreshed_actor, tenant) or auth_session_version(refreshed_actor) != session_version:
            raise ContentError('knowledge_forbidden', 403)
    if latest is None or latest['revision'] != state['revision']:
        raise ContentError('knowledge_revision_conflict', 412)
    return {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug},
        'revision': state['revision'], 'nodes': [materialize_node(n) for n in nodes],
        'text': UI['unknown'] if not nodes else '\n\n'.join(n['text'] for n in nodes),
        'selection_performed': called, 'business_writes_performed': False}

def maybe_handle_institutional_question(question, owner, session=None):
    """Existing responder integration. Tenant comes from the resolved owner, not text."""
    if not isinstance(question, (str, dict)): return None
    owner_id, tenant_id = getattr(owner, 'id', None), getattr(owner, 'tenant_id', None)
    if not owner_id: return None
    if tenant_id is not None:
        tenant = db.session.get(TenantProfile, tenant_id)
    else:
        # Resolve only an unambiguous, existing owner relationship. Never guess
        # a tenant from an incoming message or replace a contradictory identity.
        candidates = TenantProfile.query.filter(
            (TenantProfile.municipio_id == owner_id) | (TenantProfile.pyme_id == owner_id)
        ).limit(2).all()
        tenant = candidates[0] if len(candidates) == 1 else None
    if tenant is None or owner_id not in (tenant.municipio_id, tenant.pyme_id): return None
    try:
        state = read_state(tenant, public=True)
    except ContentError as error:
        if error.status == 404: return None
        return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_unavailable'}
    if session is not None and getattr(session, 'tenant_id', None) not in (None, tenant.id): return None
    text = question if isinstance(question, str) else question.get('pregunta', question.get('text', ''))
    if not isinstance(text, str): return None
    if isinstance(question, dict) and (question.get('action_id') or question.get('action')):
        text = question.get('action_id') or question.get('action')
        if not isinstance(text, str) or not text.startswith('knowledge:'): return None
    context = getattr(session, 'context_data', None) or {}
    if isinstance(context, dict) and any(context.get(key) for key in (CONTEXTO_MUNICIPIO,'contexto_municipio','contexto_pyme_v2','active_ticket_id','ticket_id')) and not text.startswith('knowledge:'):
        return None
    command = {'revision': state['revision'], 'node_id': state['bundle']['start']}
    if text.startswith('knowledge:'):
        parts = text.split(':')
        if len(parts) != 3 or parts[1] != state['revision'][:16] or parts[2] not in state['bundle']['nodes']:
            return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_stale'}
        command['node_id'] = parts[2]
    elif text.strip().lower() not in ('', 'menu', 'menú', 'inicio'):
        command['question'] = text
        previous = context.get('institutional_knowledge', {}) if isinstance(context, dict) else {}
        if previous.get('revision') == state['revision'] and previous.get('node_id') in state['bundle']['nodes']:
            command['node_id'] = previous['node_id']
    try:
        result = answer(tenant, command, public=True)
    except ContentError:
        return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_unavailable'}
    nodes = result['nodes']
    # No matched institutional answer means the existing operational handlers continue.
    if not nodes and not text.startswith('knowledge:'):
        return None
    if session is not None and nodes:
        context = deepcopy(getattr(session, 'context_data', None) or {})
        context['institutional_knowledge'] = {'revision': state['revision'], 'node_id': nodes[-1]['id']}
        session.context_data = context
    citations, choices, links = [], [], []
    for node in nodes:
        links.extend(node.get('links', []))
        for source in node['sources']:
            label = source['title'] + ' · ' + ', '.join(str(p) for p in source['pages'])
            if label not in citations: citations.append(label)
        for choice in node['actions']:
            if not any(a['target'] == choice['target'] and a['label'] == choice['label'] for a in choices): choices.append(choice)
    return {'message_body': result['text'] + ''.join('\n\n' + l['label'] + ': ' + l['url'] for l in links)
        + ('\n\n' + UI['sources'] + ':\n' + '\n'.join(citations) if citations else ''),
        'message_type': 'interactive_buttons' if choices else 'text',
        'botones': [{'texto': c['label'], 'action_id': 'knowledge:' + state['revision'][:16] + ':' + c['target']} for c in choices],
        'knowledge_sources': [s for n in nodes for s in n['sources']],
        'fuente': 'institutional_knowledge', 'context_revision': state['revision']}
