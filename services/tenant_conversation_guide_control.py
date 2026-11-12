"""Explicit, revision-checked control of an installed read-only evaluation guide."""
from copy import deepcopy
from hashlib import sha256
import json
import re
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from services.tenant_conversation_guide import CONFIG_KEY, GUIDE_ID, guide_access_descriptor
from services.accessible_support_guide import GUIDE_SHA256, load_guide

CONTRACT = 'tenant.conversation_guide_control.v1'
COMMAND = 'tenant.conversation_guide_control_command.v1'
RECEIPT = 'tenant.conversation_guide_control_save.v1'
VERSION_KEY = 'private_conversation_guide_version'
UI = {
    'heading': 'Guía privada de evaluación',
    'description': 'Habilita una guía instalada para revisar orientación con ejemplos ficticios. No aprueba contenido institucional ni activa atención real.',
    'enable': 'Habilitar guía de evaluación', 'disable': 'Deshabilitar guía',
    'confirm': 'Confirmar cambio', 'cancel': 'Cancelar', 'refresh': 'Consultar estado actual',
    'confirmation': 'El cambio afecta sólo al acceso a la guía de esta organización. No modifica usuarios, permisos, casos, fuentes ni canales.',
    'acknowledgement': 'Entiendo que el contenido sigue sujeto a validación institucional y no debe recibir datos personales.',
    'success': 'El servidor confirmó el estado de la guía.',
    'error': 'No se pudo confirmar el cambio. Consultá el estado actual antes de volver a intentarlo.',
}
class GuideControlError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)

def read_state(tenant):
    config = getattr(tenant, 'configuracion', None)
    if config is not None and not isinstance(config, dict):
        raise GuideControlError('guide_control_storage_invalid', 409)
    record = (config or {}).get(CONFIG_KEY)
    version = (config or {}).get(VERSION_KEY, 0)
    if type(version) is not int or not 0 <= version < 2147483647:
        raise GuideControlError('guide_control_storage_invalid', 409)
    if record is not None:
        valid = isinstance(record, dict) and set(record) == {'enabled', 'guide_id'}
        valid = valid and type(record.get('enabled')) is bool and record.get('guide_id') == GUIDE_ID
        if not valid:
            raise GuideControlError('guide_control_storage_invalid', 409)
    identity = {'id': tenant.id, 'slug': tenant.slug}
    canonical = json.dumps([CONTRACT, identity, version, record], sort_keys=True, separators=(',', ':'))
    revision = sha256(canonical.encode()).hexdigest()
    state = {'enabled': bool(record and record['enabled']), 'guide_id': GUIDE_ID, 'version': version}
    return state, revision

def installed_artifact():
    try:
        guide = load_guide()
        return {'guide_id': GUIDE_ID, 'guide_sha256': GUIDE_SHA256,
                'source': deepcopy(guide['source']), 'node_count': len(guide['nodes']),
                'evaluation_only': True}
    except (OSError, ValueError, TypeError, KeyError):
        return None

def build_control(tenant, *, can_edit=False, writes_blocked=False):
    state, revision = read_state(tenant)
    artifact = installed_artifact()
    permitted = can_edit is True and not writes_blocked and tenant.is_active is True
    return {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug},
            'revision': revision, 'state': state, 'evaluation_only': True, 'command_contract_version': COMMAND,
            'required_headers': {'X-Chatboc-Guide-Control': '1'},
            'installed_guide': artifact, 'can_enable': permitted and artifact is not None,
            'can_disable': permitted, 'writes_blocked': bool(writes_blocked),
            'endpoint': f'/api/admin/tenants/{tenant.slug}/conversation-guide-control',
            'access': guide_access_descriptor(tenant, can_read=True), 'ui': deepcopy(UI),
            'provider_calls_performed': False, 'operational_content_approved': False}

def decode_command(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise GuideControlError('guide_control_duplicate_field')
            result[key] = value
        return result
    try: return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError) as error:
        raise GuideControlError('guide_control_invalid_json') from error

def validate_command(data, tenant):
    required = {'contract_version', 'tenant', 'expected_revision', 'enabled', 'guide_id', 'acknowledge_evaluation_only'}
    hashes = {'expected_guide_sha256', 'expected_source_sha256'}
    if not isinstance(data, dict) or not required.issubset(data) or set(data) - required - hashes:
        raise GuideControlError('guide_control_request_invalid')
    identity = data['tenant']
    if (not isinstance(identity, dict) or set(identity) != {'id', 'slug'}
            or type(identity['id']) is not int or identity != {'id': tenant.id, 'slug': tenant.slug}):
        raise GuideControlError('guide_control_scope_mismatch')
    if (data['contract_version'] != COMMAND or type(data['enabled']) is not bool
            or data['guide_id'] != GUIDE_ID or data['acknowledge_evaluation_only'] is not True):
        raise GuideControlError('guide_control_request_invalid')
    expected = data['expected_revision']
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise GuideControlError('guide_control_revision_required', 428)
    if data['enabled']:
        artifact = installed_artifact()
        if artifact is None: raise GuideControlError('guide_control_artifact_unavailable', 503)
        if (data.get('expected_guide_sha256') != artifact['guide_sha256']
                or data.get('expected_source_sha256') != artifact['source']['sha256']):
            raise GuideControlError('guide_control_artifact_changed', 412)
    return data['enabled']

def save_control(session, tenant_model, user_model, audit_model, *, tenant_id, tenant_slug,
                 actor_id, data, authorize, writes_blocked):
    try:
        if writes_blocked(): raise GuideControlError('guide_control_maintenance', 503)
        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
        tenant = session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update().one_or_none()
        actor = session.query(user_model).filter_by(id=actor_id).populate_existing().one_or_none()
        if (tenant is None or actor is None or tenant.slug != tenant_slug
                or tenant.is_active is not True or not authorize(actor, tenant)):
            raise GuideControlError('guide_control_forbidden', 403)
        desired = validate_command(data, tenant)
        before, revision = read_state(tenant)
        if revision != data['expected_revision']:
            raise GuideControlError('guide_control_revision_conflict', 412)
        changed = before['enabled'] != desired
        if changed:
            version = before['version'] + 1
            tenant.configuracion = {**(tenant.configuracion or {}),
                CONFIG_KEY: {'enabled': desired, 'guide_id': GUIDE_ID}, VERSION_KEY: version}
            session.add(audit_model(tenant_id=tenant.id, actor_user_id=actor.id,
                event_type='conversation_guide.enabled' if desired else 'conversation_guide.disabled',
                resource_type='tenant_profile', resource_id=str(tenant.id), details={
                    'contract_version': CONTRACT, 'guide_id': GUIDE_ID, 'enabled': desired,
                    'previous_version': before['version'], 'version': version,
                    'evaluation_only': True, 'provider_calls_performed': False,
                    'guide_sha256': data.get('expected_guide_sha256') if desired else None,
                    'source_sha256': data.get('expected_source_sha256') if desired else None}))
        session.flush()
        control = build_control(tenant, can_edit=True)
        installed = control['installed_guide']
        if desired and (installed is None or installed['guide_sha256'] != data.get('expected_guide_sha256')
                        or installed['source']['sha256'] != data.get('expected_source_sha256')):
            raise GuideControlError('guide_control_artifact_changed', 412)
        receipt = {'contract_version': RECEIPT, 'tenant': control['tenant'],
                   'saved': changed, 'control': control, 'provider_calls_performed': False,
                   'operational_content_approved': False}
        json.dumps(receipt, allow_nan=False)
        if writes_blocked(): raise GuideControlError('guide_control_maintenance', 503)
        session.commit()
        return receipt
    except SQLAlchemyError as error:
        session.rollback()
        raise GuideControlError('guide_control_save_unconfirmed', 503) from error
    except BaseException:
        session.rollback()
        raise

UI.update({
    'open': 'Administrar guía de evaluación', 'loading': 'Consultando acceso a la guía…',
    'enabled_label': 'Guía habilitada', 'disabled_label': 'Guía deshabilitada',
    'source_label': 'Fuente de la guía', 'pending': 'Verificando el cambio…',
})

def control_descriptor(tenant, *, can_edit=False):
    """Discovery publishes no content and grants no authority by itself."""
    tenant_id, slug = getattr(tenant, 'id', None), getattr(tenant, 'slug', None)
    if (can_edit is not True or getattr(tenant, 'is_active', False) is not True
            or type(tenant_id) is not int or tenant_id < 1 or not isinstance(slug, str)
            or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}', slug)):
        return None
    return {'contract_version': 'tenant.conversation_guide_control_access.v1',
            'tenant': {'id': tenant_id, 'slug': slug}, 'evaluation_only': True,
            'endpoint': f'/api/admin/tenants/{slug}/conversation-guide-control', 'ui': deepcopy(UI)}
