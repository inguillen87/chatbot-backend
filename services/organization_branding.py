"""Versioned workspace palette on the existing tenant record, never provider config.

Callers supply the existing tenant authorization and strict commercial entitlement.
Only an explicit publish/restore transaction can change the persisted palette.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

KEY = 'organization_workspace_branding'
CONTRACT = 'organization.branding.v1'
STORE = 'organization.branding_store.v1'
DEFAULTS = {'enabled':False, 'primary_color':'#2563EB', 'accent_color':'#0F766E'}
PRESETS = [
    {'id':'blue','label':'Azul y teal','primary_color':'#2563EB','accent_color':'#0F766E'},
    {'id':'violet','label':'Violeta y coral','primary_color':'#6D28D9','accent_color':'#C2410C'},
    {'id':'forest','label':'Verde y pizarra','primary_color':'#166534','accent_color':'#334155'},
]

class BrandingError(ValueError):
    def __init__(self, code, status=400, message='Revisá los colores y la versión antes de publicar.'):
        self.code,self.status,self.message=code,status,message
        super().__init__(code)

def validate_values(value):
    if not isinstance(value,dict) or set(value)!=set(DEFAULTS) or type(value['enabled']) is not bool:
        raise BrandingError('branding_values_invalid')
    if any(not isinstance(value[k],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value[k]) for k in ('primary_color','accent_color')):
        raise BrandingError('branding_color_invalid',message='Usá colores con formato #RRGGBB, sin código CSS.')
    return {'enabled':value['enabled'], **{k:value[k].upper() for k in ('primary_color','accent_color')}}


def color_pair(color):
    components=[int(color[i:i+2],16)/255 for i in (1,3,5)]
    linear=[v/12.92 if v<=0.04045 else ((v+0.055)/1.055)**2.4 for v in components]
    luminance=sum(w*v for w,v in zip((0.2126,0.7152,0.0722),linear))
    black=(luminance+0.05)/0.05; white=1.05/(luminance+0.05)
    return {'background':color,'foreground':'#000000' if black>=white else '#FFFFFF',
        'contrast':round(max(black,white),3)}


def read_record(tenant):
    config=getattr(tenant,'configuracion',None)
    if config is not None and not isinstance(config,dict): raise BrandingError('branding_storage_invalid',409)
    record=(config or {}).get(KEY)
    if record is None: return {'contract_version':STORE,'version':0,'values':deepcopy(DEFAULTS),'history':[]}
    if not isinstance(record,dict) or set(record)!={'contract_version','version','values','history'} or record['contract_version']!=STORE:
        raise BrandingError('branding_storage_invalid',409)
    if type(record['version']) is not int or not 1<=record['version']<2147483647 or not isinstance(record['history'],list) or len(record['history'])>10:
        raise BrandingError('branding_storage_invalid',409)
    validate_values(record['values']); seen=set()
    for entry in record['history']:
        if not isinstance(entry,dict) or set(entry)!={'version','values'} or type(entry['version']) is not int or not 0<=entry['version']<record['version'] or entry['version'] in seen:
            raise BrandingError('branding_history_invalid',409)
        validate_values(entry['values']);seen.add(entry['version'])
    return deepcopy(record)


def revision(tenant,record):
    return sha256(json.dumps([CONTRACT,tenant.id,tenant.slug,record],sort_keys=True,separators=(',',':')).encode()).hexdigest()


def build_branding(tenant, *, can_edit=False, entitled=False, writes_blocked=False):
    if tenant is None or getattr(tenant,'is_active',False) is not True or type(tenant.id) is not int or tenant.id<1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',str(tenant.slug)):
        return None
    try: record=read_record(tenant)
    except BrandingError: return None
    reason='maintenance' if writes_blocked else 'tenant_admin_required' if can_edit is not True else 'full_plan_required' if entitled is not True else 'ready'
    messages={'maintenance':'El espacio está en modo consulta temporal. No se publica mientras dure el mantenimiento.',
        'tenant_admin_required':'Necesitás permiso de administración en esta organización para publicar.',
        'full_plan_required':'La personalización del espacio requiere el plan Full registrado en el servidor.',
        'ready':'Podés previsualizar y publicar la paleta de esta organización.'}
    values=record['values'];appearance={'active':values['enabled'] and entitled is True,
        'primary':color_pair(values['primary_color']),'accent':color_pair(values['accent_color'])}
    return {'contract_version':CONTRACT,'tenant':{'id':tenant.id,'slug':tenant.slug},
        'revision':revision(tenant,record),'version':record['version'],'values':values,
        'history':list(reversed(record['history'])),'can_edit':reason=='ready','reason_code':reason,
        'message':messages[reason],'appearance':appearance,'presets':deepcopy(PRESETS),
        'save_endpoint':f'/api/admin/tenants/{tenant.slug}/config','provider_calls_performed':False,
        'heading':'Paleta del espacio','scope_note':'Se aplica al perfil institucional y centro de implementación. No cambia dominios, PWA, WhatsApp ni plantillas aprobadas.'}


def build_workspace_appearance(tenant, *, entitled=False):
    brand=build_branding(tenant,entitled=entitled)
    if brand is None: return None
    return {'contract_version':'organization.workspace_appearance.v1','tenant':brand['tenant'],
        'version':brand['version'],'appearance':brand['appearance']}

def save_branding(session, tenant_model, user_model, audit_model, *, tenant_id, actor_id, data, authorize, entitlement):
    try:
        if not isinstance(data,dict) or set(data)!={'organization_branding','expected_revision'}:
            raise BrandingError('branding_request_invalid')
        expected=data['expected_revision'];operation=data['organization_branding']
        if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):
            raise BrandingError('branding_revision_required',428,'Actualizá el estado antes de publicar.')
        if not isinstance(operation,dict): raise BrandingError('branding_request_invalid')
        if session.get_bind().dialect.name=='postgresql': session.execute(text("SET LOCAL lock_timeout = '5s'"))
        tenant=session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update().one_or_none()
        actor=session.query(user_model).filter_by(id=actor_id).populate_existing().one_or_none()
        if tenant is None or tenant.is_active is not True or actor is None or not authorize(actor,tenant):
            raise BrandingError('branding_forbidden',403,'No tenés permiso para publicar en esta organización.')
        if entitlement(tenant) is not True:
            raise BrandingError('branding_full_required',403,'Esta función requiere Full; no se activó ni cobró otro plan.')
        record=read_record(tenant)
        if revision(tenant,record)!=expected:
            raise BrandingError('branding_revision_conflict',412,'Otra persona cambió la marca. Tu borrador sigue disponible; actualizá la versión antes de publicar.')
        kind=operation.get('operation')
        if kind=='publish' and set(operation)=={'operation','values'}:
            values=validate_values(operation['values'])
        elif kind=='restore' and set(operation)=={'operation','version'} and type(operation['version']) is int:
            previous=next((v for v in record['history'] if v['version']==operation['version']),None)
            if previous is None: raise BrandingError('branding_version_unavailable',409,'La versión solicitada ya no está disponible.')
            values=validate_values(previous['values'])
        else: raise BrandingError('branding_operation_invalid')
        changed=values!=record['values'] or kind=='restore'
        if changed:
            before=record['version']
            history=(record['history']+[{'version':before,'values':record['values']}])[-10:]
            new={'contract_version':STORE,'version':before+1,'values':values,'history':history}
            tenant.configuracion={**(tenant.configuracion or {}),KEY:new}
            session.add(audit_model(tenant_id=tenant.id,actor_user_id=actor.id,event_type='organization.branding.'+kind,
                resource_type='tenant_profile',resource_id=str(tenant.id),details={'contract_version':CONTRACT,
                    'previous_version':before,'version':before+1,'restored_from':operation.get('version'),
                    'provider_calls_performed':False}))
        session.flush()
        brand=build_branding(tenant,can_edit=True,entitled=True)
        if brand is None: raise BrandingError('branding_storage_invalid',409)
        result={'contract_version':'organization.branding_save.v1','saved':changed,
            'tenant':brand['tenant'],'brand':brand,'provider_calls_performed':False}
        json.dumps(result,allow_nan=False);session.commit()
        return result
    except SQLAlchemyError as error:
        session.rollback()
        raise BrandingError('branding_save_unconfirmed',503,'No pudimos confirmar la publicación. Consultá la versión actual antes de reintentar.') from error
    except BaseException:
        session.rollback();raise
