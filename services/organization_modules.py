"""Versioned setup goals, not authorization flags or runtime channel switches."""
from copy import deepcopy
from hashlib import sha256
import json
import re
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from services.organization_workspace import build_organization_workspace

KEY = 'organization_setup_modules'
CONTRACT = 'organization.setup_modules.v1'
CATALOG_VERSION = 1
CATALOG = (
    {'id':'whatsapp','label':'WhatsApp y plantillas','description':'Preparar el canal y sus mensajes sin reemplazar la conexión existente.','requires':[]},
    {'id':'catalog','label':'Catálogo de productos o servicios','description':'Organizar lo que las personas pueden consultar y pedir.','requires':[]},
    {'id':'payments','label':'Cobros y pedidos','description':'Preparar cobros asociados al catálogo. No inicia pagos ni conecta una pasarela.','requires':['catalog']},
    {'id':'surveys','label':'Encuestas y participación','description':'Preparar consultas y seguimiento de la experiencia.','requires':[]},
    {'id':'territory','label':'Territorio y mapas','description':'Preparar la jurisdicción y atención territorial de un gobierno o municipio.','requires':[]},
)
UI = {'heading':'Elegí qué preparar en este espacio','description':'Personalizá los pasos de implementación. No cambia tu plan, permisos ni desactiva servicios que ya funcionan.',
    'save':'Guardar selección','saving':'Verificando guardado…','refresh':'Leer versión actual','reset':'Descartar edición',
    'dirty':'Tenés una selección pendiente de guardar.','success':'La selección quedó confirmada por el servidor.',
    'error':'No pudimos confirmar la operación. Conservamos tu edición; revisá la versión actual antes de continuar.',
    'conflict':'Otra persona cambió la selección. Revisá la versión guardada antes de volver a publicar.',
    'loading':'Consultando las funciones de esta organización…','missing':'La configuración de módulos todavía no está disponible.',
    'current':'Selección guardada','draft':'Tu selección','review':'Usar mi edición sobre la versión revisada',
    'dependency':'Para preparar esta función también debe estar seleccionada su dependencia.',
    'confirm':'¿Guardar esta selección?','confirm_detail':'Sólo cambiarán los pasos de preparación. Los datos, conexiones y suscripciones se conservan.',
    'cancel':'Seguir editando','discard_title':'¿Descartar la selección pendiente?','discard_detail':'Volverás a la última selección leída. No se enviará ningún cambio al servidor.'}

class ModuleSelectionError(ValueError):
    def __init__(self, code, status=400):
        self.code,self.status=code,status
        super().__init__(code)

def policy(tenant):
    context=build_organization_workspace(tenant)
    if context is None: raise ModuleSelectionError('module_tenant_unavailable',403)
    government=context['organization_type'] in ('municipio','gobierno')
    offered=[m for m in CATALOG if government or m['id']!='territory']
    defaults=['whatsapp','surveys','territory'] if government else ['whatsapp','catalog','payments']
    if context['organization_type']=='organizacion': defaults=['whatsapp']
    return context,offered,defaults

def validate_selection(selected, offered):
    allowed = {item['id'] for item in offered}
    if not isinstance(selected, list):
        raise ModuleSelectionError('module_selection_invalid')
    if len(selected) > len(offered):
        raise ModuleSelectionError('module_selection_invalid')
    for key in selected:
        if not isinstance(key, str) or key not in allowed:
            raise ModuleSelectionError('module_selection_invalid')
    if len(set(selected)) != len(selected):
        raise ModuleSelectionError('module_selection_invalid')
    for item in offered:
        if item['id'] in selected:
            if not set(item['requires']).issubset(selected):
                raise ModuleSelectionError('module_dependency_required', 409)
    return [item['id'] for item in offered if item['id'] in selected]

def record(tenant):
    _, offered, defaults = policy(tenant)
    config = getattr(tenant, 'configuracion', None)
    if config is not None and not isinstance(config, dict):
        raise ModuleSelectionError('module_storage_invalid', 409)
    saved = (config or {}).get(KEY)
    if saved is None:
        return {'version': 0, 'selected': defaults, 'catalog_version': CATALOG_VERSION}
    fields = {'version', 'selected', 'catalog_version'}
    if not isinstance(saved, dict) or set(saved) != fields:
        raise ModuleSelectionError('module_storage_invalid', 409)
    if type(saved['version']) is not int or not 1 <= saved['version'] < 2147483647:
        raise ModuleSelectionError('module_storage_invalid', 409)
    if type(saved['catalog_version']) is not int or saved['catalog_version'] != CATALOG_VERSION:
        raise ModuleSelectionError('module_storage_invalid', 409)
    return {**deepcopy(saved), 'selected': validate_selection(saved['selected'], offered)}


def revision(tenant, saved):
    value = [CONTRACT, tenant.id, tenant.slug, getattr(tenant, 'tipo', None), saved]
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return sha256(encoded).hexdigest()

def build_modules(tenant, *, can_edit=False, entitled=False, writes_blocked=False):
    try:
        context, offered, defaults = policy(tenant)
        saved = record(tenant)
    except ModuleSelectionError:
        return None
    reason = ('maintenance' if writes_blocked else 'tenant_admin_required' if can_edit is not True
              else 'full_plan_required' if entitled is not True else 'ready')
    messages = {
        'maintenance': 'El espacio está en modo consulta por mantenimiento.',
        'tenant_admin_required': 'Necesitás permiso de administración en esta organización.',
        'full_plan_required': 'La selección personalizada requiere Full. No se cambia ni cobra otro plan.',
        'ready': 'Podés guardar la preparación que necesita esta organización.',
    }
    return {
        'contract_version': CONTRACT, 'tenant': context['tenant'],
        'revision': revision(tenant, saved), 'version': saved['version'],
        'selected': saved['selected'], 'catalog_version': CATALOG_VERSION,
        'catalog': deepcopy(offered), 'defaults': defaults,
        'can_edit': reason == 'ready', 'reason_code': reason, 'message': messages[reason],
        'ui': deepcopy(UI), 'save_endpoint': f'/api/admin/tenants/{tenant.slug}/config',
        'provider_calls_performed': False, 'changes_permissions': False,
    }


def save_modules(session, tenant_model, user_model, audit_model, *, tenant_id, actor_id,
                 data, authorize, entitlement):
    try:
        if not isinstance(data, dict) or set(data) != {'organization_modules', 'expected_revision'}:
            raise ModuleSelectionError('module_request_invalid')
        expected = data['expected_revision']
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ModuleSelectionError('module_revision_required', 428)
        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
        tenant = session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update().one_or_none()
        actor = session.query(user_model).filter_by(id=actor_id).populate_existing().one_or_none()
        if tenant is None or tenant.is_active is not True or actor is None or not authorize(actor, tenant):
            raise ModuleSelectionError('module_forbidden', 403)
        if entitlement(tenant) is not True:
            raise ModuleSelectionError('module_full_required', 403)
        current = record(tenant)
        if revision(tenant, current) != expected:
            raise ModuleSelectionError('module_revision_conflict', 412)
        _, offered, _ = policy(tenant)
        operation = data['organization_modules']
        if not isinstance(operation, dict) or set(operation) != {'selected'}:
            raise ModuleSelectionError('module_request_invalid')
        selected = validate_selection(operation['selected'], offered)
        changed = current['version'] == 0 or selected != current['selected']
        if changed:
            updated = {'version': current['version'] + 1, 'selected': selected, 'catalog_version': CATALOG_VERSION}
            tenant.configuracion = {**(tenant.configuracion or {}), KEY: updated}
            session.add(audit_model(tenant_id=tenant.id, actor_user_id=actor.id,
                event_type='organization.setup_modules.saved', resource_type='tenant_profile',
                resource_id=str(tenant.id), details={'version': updated['version'],
                'previous_version': current['version'], 'selected': selected, 'provider_calls_performed': False}))
        session.flush()
        modules = build_modules(tenant, can_edit=True, entitled=True)
        if modules is None:
            raise ModuleSelectionError('module_storage_invalid', 409)
        result = {'contract_version': 'organization.setup_modules_save.v1', 'saved': changed,
                  'tenant': modules['tenant'], 'modules': modules, 'provider_calls_performed': False}
        json.dumps(result, allow_nan=False)
        session.commit()
        return result
    except SQLAlchemyError as error:
        session.rollback()
        raise ModuleSelectionError('module_save_unconfirmed', 503) from error
    except BaseException:
        session.rollback()
        raise


def apply_setup_selection(tenant, definitions):
    """Only tailor v2 preparation steps. Keep all runtime/channel checks intact."""
    saved = record(tenant)
    if saved['version'] == 0:
        return definitions
    selected = saved['selected']
    mapping = {'whatsapp': ('whatsapp', 'templates'), 'catalog': ('catalog_marketplace',),
               'payments': ('payments_checkout',), 'surveys': ('analytics_surveys',),
               'territory': ('territorial_intelligence',)}
    managed = {source for sources in mapping.values() for source in sources}
    result = deepcopy(definitions)
    for step in result:
        sources = [source for source in step['source_ids'] if source not in managed]
        options = ('whatsapp',) if step['id'] == 'channels' else (
            ('catalog', 'payments') if step['id'] == 'knowledge' else (
            ('surveys', 'territory') if step['id'] == 'validation_release' else ()))
        for key in options:
            if key in selected:
                sources.extend(mapping[key])
        step['source_ids'] = tuple(sources)
        if step['id'] == 'knowledge':
            step['label'] = 'Contenidos y funciones elegidas'
            step['description'] = 'Revisá los contenidos y las funciones incluidas en tu selección guardada.'
    return result
