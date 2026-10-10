"""Persisted institution knowledge used by the app and the existing chat handler."""
from copy import deepcopy
from sqlalchemy import select
from flask import current_app
from models import db, TenantProfile, TenantConfig, AuditEvent, User
from cutover_writer_fence import cutover_writer_fence_enabled
from services.constants import CONTEXTO_MUNICIPIO, ConversationState
from services.source_event_context import SOURCE_EVENT_CONTEXT_FIELDS, normalize_source_event_context
from utils.tenant_admin_access import can_manage_tenant_control_plane
from utils.auth_helpers import is_user_auth_disabled
from services.institutional_assistant_content import (
    ContentError, CONTRACT, normalize_bundle, overview, digest, select_nodes, materialize_node,
)
KEY, CHANNEL = 'institutional_assistant', 'knowledge'
UI = {
    'heading': 'Información y orientación', 'description': 'Consultá por tema o escribí tu pregunta.',
    'topics': 'Temas de consulta', 'sources': 'Documentos y fuentes', 'source_details': 'Consultar fuentes',
    'question': 'Escribí tu consulta', 'placeholder': '¿Sobre qué necesitás información?',
    'send': 'Consultar', 'back': 'Volver', 'home': 'Todos los temas', 'loading': 'Consultando la información…',
    'more_options': 'Más opciones', 'previous_options': 'Opciones anteriores',
    'options_page': 'Opciones: grupo {current} de {total}',
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
_CHANNEL_UI_KEYS = ('more_options', 'previous_options', 'options_page', 'large_text', 'source_details')

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

def workspace(tenant, state, *, editable=False, public=False):
    data = {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug, 'name': tenant.nombre},
        'revision': state['revision'] if state else None, 'visibility': state['visibility'] if state else 'empty',
        'can_edit': editable, 'ui': deepcopy(UI), 'knowledge': None}
    if state:
        bundle = state['bundle']
        data['knowledge'] = {**overview(bundle, public=public), 'version': bundle['version'], 'initial': materialize_node(bundle['nodes'][bundle['start']], public=public)}
        if state['visibility'] == 'public':
            from services.institutional_assistant_audio import audio_reading_capability
            capability = audio_reading_capability()
            if capability is not None: data['audio_reading'] = capability
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
        if not current.is_active or is_user_auth_disabled(fresh_actor) or not can_manage_tenant_control_plane(fresh_actor, current):
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
    if actor_id and (is_user_auth_disabled(actor) or not can_manage_tenant_control_plane(actor, tenant)):
        raise ContentError('knowledge_forbidden', 403)
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
        from services.auth_session_lifecycle import request_auth_session_active
        refreshed_actor = db.session.get(User, actor_id, populate_existing=True)
        if (refreshed_actor is None or is_user_auth_disabled(refreshed_actor)
            or not can_manage_tenant_control_plane(refreshed_actor, tenant)
            or auth_session_version(refreshed_actor) != session_version
            or not request_auth_session_active(actor_id)):
            raise ContentError('knowledge_forbidden', 403)
    if latest is None or latest['revision'] != state['revision']:
        raise ContentError('knowledge_revision_conflict', 412)
    return {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug},
        'revision': state['revision'], 'nodes': [materialize_node(n, public=public) for n in nodes],
        'text': UI['unknown'] if not nodes else '\n\n'.join(n['text'] for n in nodes),
        'selection_performed': called, 'business_writes_performed': False}

def _channel_source_label(source):
    label = source['title']
    if source.get('format') == 'text':
        label += ' · texto extraído'
    elif source.get('format') == 'jpeg':
        label += ' · imagen'
    else:
        label += ' · ' + ', '.join(str(p) for p in source['pages'])
    if source.get('printed_year') is not None:
        label += ' · edición ' + str(source['printed_year'])
    if source.get('review_status') == 'conflict':
        label += ' · fuentes por conciliar'
    elif source.get('review_status') in ('unreviewed', 'needs_review'):
        label += ' · revisión pendiente'
    return label


_EMPTY_MUNICIPAL_FORM_FIELDS = frozenset((
    'categoria', 'descripcion', 'ubicacion', 'nombre_ciudadano',
    'telefono_ciudadano', 'email_ciudadano', 'dni_ciudadano',
    'nombre', 'telefono', 'email', 'dni',
))


def _empty_known_municipal_form(value):
    # Unknown fields and malformed values remain operational, even when falsy.
    return (isinstance(value, dict) and set(value) <= _EMPTY_MUNICIPAL_FORM_FIELDS
        and all(item is None or (isinstance(item, str) and not item.strip())
            for item in value.values()))


def _inert_initial_municipal_context(value):
    """Only the known empty form and normalized routing identifiers are inert."""
    allowed = set(SOURCE_EVENT_CONTEXT_FIELDS) | {
        'estado_conversacion', 'datos_reclamo', 'historial_conversacion', 'id_ticket_creado'}
    if not set(value) <= allowed or value.get('estado_conversacion') not in (None, 'inicio'):
        return False
    source = {key: value[key] for key in SOURCE_EVENT_CONTEXT_FIELDS if key in value}
    if source != normalize_source_event_context(source):
        return False
    return (value.get('id_ticket_creado') is None
        and ('datos_reclamo' not in value or _empty_known_municipal_form(value['datos_reclamo']))
        and ('historial_conversacion' not in value or isinstance(value['historial_conversacion'], list)))


def _active_operational_context(context):
    """General municipal history is not an unfinished operation."""
    if not isinstance(context, dict): return False
    if any(context.get(key) for key in (
        'active_ticket_id', 'ticket_id', 'contexto_pyme_v2',
        'human_chat_in_progress', 'live_chat_ticket_id', 'live_chat_estado',
        'live_chat_socket_room', 'live_chat_status',
    )):
        return True
    for key in (CONTEXTO_MUNICIPIO, 'contexto_municipio'):
        value = context.get(key)
        if not value: continue
        if not isinstance(value, dict):
            return True
        # Idempotent institutional turns also bind routing identifiers here.
        # Their presence does not create an operation or a conversation state.
        if _inert_initial_municipal_context(value):
            continue
        # Preserve unknown/legacy states and every waiting flow.
        if value.get('estado_conversacion') != ConversationState.CONVERSACION_GENERAL_LLM.name:
            return True
        if any(value.get(field) for field in (
            'active_ticket_id', 'ticket_id', 'id_ticket_creado', 'reclamo_flow_v2',
            'expected_fields_llm_reclamo', 'expected_fields_llm_sugerencia',
            'esperando_info_llm', 'esperando_info_llm_reclamo', 'esperando_info_llm_sugerencia',
            'human_chat_in_progress', 'live_chat_ticket_id', 'live_chat_estado',
            'live_chat_socket_room', 'live_chat_status',
        )):
            return True
        for field in ('datos_parciales_llm_reclamo', 'datos_parciales_llm_sugerencia'):
            if value.get(field) and not _empty_known_municipal_form(value[field]):
                return True
    return False


def _institutional_reply_code(text):
    """Accept only numeric menu input, including a single Unicode keycap digit."""
    value = text.strip()
    if value.isascii() and value.isdecimal():
        return value
    # Keyboards may emit the keycap with or without the emoji variation selector.
    # This is an input format, not an inferred intent; advertised choices below
    # remain the authority for the current tenant, node and revision.
    if len(value) in (2, 3) and value[0] in '0123456789' and value[1:] in ('\u20e3', '\ufe0f\u20e3'):
        return value[0]
    return None


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
    # Preserve the existing municipal alias for iniciar_reclamo. Match the
    # complete command only; questions and negations remain corpus questions.
    if (tenant.tipo == 'municipio' and tenant.municipio_id == owner_id
        and text.strip().casefold() == 'iniciar un reclamo'):
        return None
    context = getattr(session, 'context_data', None) or {}
    if _active_operational_context(context) and not text.startswith('knowledge:'):
        return None
    command = {'revision': state['revision'], 'node_id': state['bundle']['start']}
    previous = context.get('institutional_knowledge', {}) if isinstance(context, dict) else {}
    if not isinstance(previous, dict): previous = {}
    if text.startswith('knowledge:'):
        parts = text.split(':')
        if len(parts) != 3 or parts[1] != state['revision'][:16] or parts[2] not in state['bundle']['nodes']:
            return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_stale'}
        command['node_id'] = parts[2]
    elif text.strip().lower() not in ('', 'menu', 'menú', 'inicio', '__init__'):
        # Text-only WhatsApp menus carry an explicit reply code. Resolve it
        # solely against the displayed node and current persisted revision;
        # free-form language still goes through the existing LLM selector.
        reply_code = _institutional_reply_code(text)
        is_reply_code = reply_code is not None
        known_context = previous.get('revision') == state['revision'] and previous.get('node_id') in state['bundle']['nodes']
        if is_reply_code:
            advertised = previous.get('reply_choices')
            if not known_context or previous.get('reply_node_id') != previous['node_id'] or not isinstance(advertised, dict):
                return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_stale'}
            actions = state['bundle']['nodes'][previous['node_id']]['actions']
            selected = next((a for a in actions if a['code'] == reply_code and a['target'] == advertised.get(reply_code)), None)
            if selected is None:
                return {'message_body': UI['unknown'], 'fuente': 'institutional_knowledge_unknown_choice'}
            command['node_id'] = selected['target']
        else:
            command['question'] = text
        if known_context and not is_reply_code:
            command['node_id'] = previous['node_id']
    try:
        result = answer(tenant, command, public=True)
    except ContentError:
        return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_unavailable'}
    nodes = result['nodes']
    unknown_question = not nodes and command.get('question') is not None
    if unknown_question:
        # A published corpus remains the authority for informational questions.
        # Empty selection must not ask another model to invent a free-form answer.
        # Re-read the canonical start menu with the same revision, without selection.
        try:
            result = answer(tenant, {'revision': state['revision'],
                'node_id': state['bundle']['start']}, public=True)
        except ContentError:
            return {'message_body': UI['error'], 'fuente': 'institutional_knowledge_unavailable'}
        nodes = result['nodes']
    citations, choices, links = [], [], []
    for node in nodes:
        links.extend(node.get('links', []))
        for source in node['sources']:
            label = _channel_source_label(source)
            if label not in citations: citations.append(label)
        for choice in node['actions']:
            if not any(a['target'] == choice['target'] and a['label'] == choice['label'] for a in choices): choices.append(choice)
    reply_choices = {c['code']: c['target'] for c in choices
        if len(nodes) == 1 and len(c['code']) <= 3 and c['code'].isascii() and c['code'].isdecimal()}
    if session is not None and nodes:
        context = deepcopy(getattr(session, 'context_data', None) or {})
        context['institutional_knowledge'] = {'revision': state['revision'], 'node_id': nodes[-1]['id'],
            'reply_node_id': nodes[0]['id'] if len(nodes) == 1 else None, 'reply_choices': reply_choices}
        session.context_data = context
    buttons = [{'texto': c['label'], 'action_id': 'knowledge:' + state['revision'][:16] + ':' + c['target'],
        **({'reply_code': c['code']} if c['code'] in reply_choices else {})} for c in choices]
    if unknown_question:
        return {'message_body': UI['unknown'], 'botones': buttons,
            'message_type': 'interactive_buttons' if buttons else 'text',
            'fuente': 'institutional_knowledge_unknown_question',
            'context_revision': state['revision']}
    from services.institutional_assistant_audio import audio_reading_capability
    capability = audio_reading_capability()
    return {'message_body': result['text'] + ''.join('\n\n' + l['label'] + ': ' + l['url'] for l in links)
        + ('\n\n' + UI['sources'] + ':\n' + '\n'.join(citations) if citations else ''),
        'message_type': 'interactive_buttons' if choices else 'text',
        'botones': buttons,
        'knowledge_sources': [s for n in nodes for s in n['sources']],
        'knowledge_tenant': deepcopy(result['tenant']), 'knowledge_nodes': deepcopy(nodes),
        'knowledge_ui': {key: UI[key] for key in _CHANNEL_UI_KEYS},
        **({'knowledge_audio_reading': capability} if capability is not None else {}),
        'fuente': 'institutional_knowledge', 'context_revision': state['revision']}
