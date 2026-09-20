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
STORE = 'organization.setup_modules_store.v1'
UI.update({'ready':'Seleccionado para preparar','idle':'No seleccionado','requires':'Requiere',
    'full':'La personalización de los pasos requiere el plan Full registrado en el servidor.',
    'permission':'Necesitás permiso de administración para cambiar esta selección.',
    'maintenance':'El espacio está en modo consulta temporal. No se guardará durante el mantenimiento.',
    'allowed':'Podés elegir qué preparar sin modificar tus servicios activos.',
    'saved_version':'Versión guardada','defaults':'Selección sugerida para tu actividad',
    'selected_count':'Funciones seleccionadas','compare':'Revisá la selección actual y tu edición',
    'review_note':'Aceptar la comparación sólo prepara la edición; no guarda ni reintenta la operación.',
    'review_ready':'La revisión quedó aplicada a tu borrador. Confirmá para guardar.',
    'no_changes':'La selección ya coincide con la versión guardada.',
    'discard_confirm':'Descartar selección','continue':'Continuar editando','confirmed':'Confirmar selección',
    'scope_note':'Elegir una función ajusta los pasos de implementación. No cambia permisos, números, datos ni conexiones existentes.',
    'legacy_note':'Hasta guardar, se usa la sugerencia de la plataforma para esta actividad.',
    'denied':'El acceso cambió. Los datos de esta organización ya no se muestran.'})


def normalized_selection(value, offered):
    ids = [item['id'] for item in offered]
    if not isinstance(value, list) or len(value)>len(ids) or any(type(item) is not str or item not in ids for item in value):
        raise ModuleSelectionError('module_selection_invalid')
    if len(set(value))!=len(value): raise ModuleSelectionError('module_selection_duplicate')
    if any(item['id'] in value and not set(item['requires']).issubset(value) for item in offered):
        raise ModuleSelectionError('module_dependency_required')
    return [item for item in ids if item in value]


def read_selection(tenant):
    context, offered, defaults = policy(tenant)
    config = getattr(tenant, 'configuracion', None)
    if config is not None and not isinstance(config, dict): raise ModuleSelectionError('module_storage_invalid',409)
    record = (config or {}).get(KEY)
    if record is None:
        return context, offered, {'catalog_version':CATALOG_VERSION,'version':0,'selected':normalized_selection(defaults,offered)}
    if (not isinstance(record,dict) or set(record)!={'catalog_version','version','selected'}
            or type(record['catalog_version']) is not int or record['catalog_version']!=CATALOG_VERSION or type(record['version']) is not int
            or not 1<=record['version']<2147483647):
        raise ModuleSelectionError('module_storage_invalid',409)
    try: selected = normalized_selection(record['selected'], offered)
    except ModuleSelectionError as error: raise ModuleSelectionError('module_storage_invalid',409) from error
    return context, offered, {**record,'selected':selected}


def selection_revision(context, record):
    return sha256(json.dumps([CONTRACT,CATALOG_VERSION,context['tenant'],context['organization_type'],record],
        sort_keys=True,separators=(',',':')).encode()).hexdigest()


def build_module_selection(tenant, *, can_edit=False, entitled=False, writes_blocked=False):
    try: context,offered,record = read_selection(tenant)
    except ModuleSelectionError: return None
    reason='maintenance' if writes_blocked is True else 'permission' if can_edit is not True else 'full' if entitled is not True else 'allowed'
    return {'contract_version':CONTRACT,'catalog_version':CATALOG_VERSION,'tenant':context['tenant'],
        'organization_type':context['organization_type'],'revision':selection_revision(context,record),
        'version':record['version'],'source':'saved' if record['version'] else 'defaults',
        'selected':record['selected'],'catalog':deepcopy(offered),'ui':deepcopy(UI),
        'can_edit':reason=='allowed','reason_code':reason,'message':UI[reason],
        'save_endpoint':f"/api/admin/tenants/{context['tenant']['slug']}/config",
        'provider_calls_performed':False,'changes_runtime_access':False}


def save_module_selection(session, tenant_model, user_model, audit_model, *, tenant_id, actor_id, data, authorize, entitlement):
    try:
        if not isinstance(data,dict) or set(data)!={'organization_modules','expected_revision'}:
            raise ModuleSelectionError('module_request_invalid')
        expected=data['expected_revision']
        if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):
            raise ModuleSelectionError('module_revision_required',428)
        operation=data['organization_modules']
        if not isinstance(operation,dict) or set(operation)!={'selected'}: raise ModuleSelectionError('module_request_invalid')
        if session.get_bind().dialect.name=='postgresql': session.execute(text("SET LOCAL lock_timeout = '5s'"))
        tenant=session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update().one_or_none()
        actor=session.query(user_model).filter_by(id=actor_id).populate_existing().one_or_none()
        if tenant is None or actor is None or tenant.is_active is not True or not authorize(actor,tenant):
            raise ModuleSelectionError('module_forbidden',403)
        if entitlement(tenant) is not True: raise ModuleSelectionError('module_full_required',403)
        context,offered,record=read_selection(tenant)
        if selection_revision(context,record)!=expected: raise ModuleSelectionError('module_revision_conflict',412)
        selected=normalized_selection(operation['selected'],offered)
        changed=record['version']==0 or selected!=record['selected']
        if changed:
            updated={'catalog_version':CATALOG_VERSION,'version':record['version']+1,'selected':selected}
            tenant.configuracion={**(tenant.configuracion or {}),KEY:updated}
            session.add(audit_model(tenant_id=tenant.id,actor_user_id=actor.id,
                event_type='organization.setup_modules.saved',resource_type='tenant_profile',resource_id=str(tenant.id),
                details={'contract_version':CONTRACT,'previous_version':record['version'],'version':updated['version'],
                    'selected':selected,'changes_runtime_access':False,'provider_calls_performed':False}))
        session.flush()
        snapshot=build_module_selection(tenant,can_edit=True,entitled=True)
        if snapshot is None: raise ModuleSelectionError('module_storage_invalid',409)
        result={'contract_version':'organization.setup_modules_save.v1','saved':changed,'tenant':snapshot['tenant'],
            'selection':snapshot,'provider_calls_performed':False,'changes_runtime_access':False}
        json.dumps(result,allow_nan=False);session.commit()
        return result
    except SQLAlchemyError as error:
        session.rollback();raise ModuleSelectionError('module_save_unconfirmed',503) from error
    except BaseException:
        session.rollback();raise
