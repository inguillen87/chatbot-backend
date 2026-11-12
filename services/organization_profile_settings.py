"""Versioned institutional profile on existing tenant/owner records.

Personal identity, entitlements, domains and provider credentials are never
accepted here. Callers supply resolved models and the existing authorization.
"""
from copy import deepcopy
from hashlib import sha256
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit
from sqlalchemy import text, or_
from sqlalchemy.exc import SQLAlchemyError
from services.organization_type_presentation import organization_type_descriptor
from services.whatsapp_receipt_branding import public_whatsapp_receipt_image_url

CONTRACT = 'organization.profile_settings.v1'
LIMITS = {'nombre_empresa':150, 'telefono':20, 'direccion':200, 'ciudad':100,
          'provincia':100, 'pais':100, 'link_web':255, 'logo_url':255, 'actividad':100}
CONFIG_TEXT_FIELDS = {'actividad': 'organization_profile_activity'}
RECEIPT_HEADER_KEY = 'whatsapp_receipt_header'
FIELDS = frozenset((*LIMITS, 'latitud', 'longitud', 'horario_json', RECEIPT_HEADER_KEY))
DAYS = ('Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo')
HOURS_KEY = 'organization_profile_hours'


def validate_receipt_header(value):
    if value is None:
        return None  # Explicitly restore the tenant's legacy inheritance.
    if (not isinstance(value, dict) or set(value) != {'enabled', 'image_url'}
            or type(value['enabled']) is not bool or not isinstance(value['image_url'], str)):
        raise ProfileSettingsError('profile_receipt_header_invalid')
    if any(ord(c) < 32 for c in value['image_url']):
        raise ProfileSettingsError('profile_receipt_header_url_invalid')
    image = value['image_url'].strip()
    if not image:
        if value['enabled']:
            raise ProfileSettingsError('profile_receipt_header_image_required')
        return {'enabled': False, 'image_url': ''}
    public_image = public_whatsapp_receipt_image_url(value['image_url'])
    if public_image is None:
        raise ProfileSettingsError('profile_receipt_header_url_invalid',
            message='Ingresá una imagen pública HTTPS PNG, JPG o WebP sin credenciales ni parámetros.')
    return {'enabled': value['enabled'], 'image_url': public_image}


def receipt_header_value(config):
    raw = config.get(RECEIPT_HEADER_KEY)
    if raw is None:
        return None
    if isinstance(raw, dict) and set(raw) in ({'image_url'}, {'enabled', 'image_url'}):
        legacy = {'enabled': raw.get('enabled', True), 'image_url': raw.get('image_url', '')}
        try:
            return validate_receipt_header(legacy)
        except ProfileSettingsError:
            pass
    # Reflect the effective fail-closed state without exposing malformed URLs.
    return {'enabled': False, 'image_url': ''}

class ProfileSettingsError(ValueError):
    def __init__(self, code, status=400, message=None):
        self.code, self.status = code, status
        self.message = message or 'Revisá los datos de la organización antes de guardar.'
        super().__init__(code)

def owner_id(tenant):
    owners = [v for v in (getattr(tenant,'municipio_id',None),getattr(tenant,'pyme_id',None)) if v is not None]
    if len(owners)!=1 or isinstance(owners[0],bool) or not isinstance(owners[0],int) or owners[0]<1:
        raise ProfileSettingsError('organization_owner_unavailable',409)
    return owners[0]


def profile_values(tenant, owner):
    values = {key: str(getattr(owner,key,None) or '') for key in LIMITS}
    values['nombre_empresa'] = str(getattr(tenant,'nombre',None) or values['nombre_empresa'])
    values['logo_url'] = str(getattr(tenant,'logo_url',None) or values['logo_url'])
    values.update({key:getattr(owner,key,None) for key in ('latitud','longitud')})
    config = tenant.configuracion if isinstance(tenant.configuracion,dict) else {}
    sector = getattr(owner, 'rubro', None)
    legacy_activity = getattr(sector, 'nombre', None) or getattr(sector, 'clave', None) or ''
    for field, config_key in CONFIG_TEXT_FIELDS.items():
        stored = config.get(config_key, legacy_activity)
        values[field] = stored if isinstance(stored, str) else ''
    hours = config.get(HOURS_KEY, getattr(owner,'horario_json',None))
    values['horario_json'] = deepcopy(hours) if isinstance(hours,list) else []
    values[RECEIPT_HEADER_KEY] = receipt_header_value(config)
    return values


def profile_revision(tenant, owner, values):
    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    receipt_state = {'present': RECEIPT_HEADER_KEY in config, 'raw': config.get(RECEIPT_HEADER_KEY)}
    canonical = json.dumps([CONTRACT,tenant.id,tenant.slug,owner.id,values,receipt_state],
        sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    return sha256(canonical.encode()).hexdigest()


def profile_editability(*, can_edit=False, writes_blocked=False):
    if writes_blocked is True:
        return {'mode':'read_only', 'reason_code':'maintenance',
            'message':'El sistema está temporalmente en modo consulta. Tus permisos no cambiaron; volvé a consultar el estado cuando finalice el mantenimiento.'}
    if can_edit is not True:
        return {'mode':'read_only', 'reason_code':'tenant_admin_required',
            'message':'Podés consultar este perfil. Para modificarlo necesitás un permiso de administración en esta organización.'}
    return {'mode':'editable', 'reason_code':'ready',
        'message':'Tenés permiso para editar el perfil de esta organización.'}


def build_profile_settings(tenant, owner, *, can_edit=False, writes_blocked=False):
    if tenant is None or owner is None or getattr(tenant,'is_active',False) is not True:
        return None
    identifier = getattr(tenant, 'id', None)
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
        return None
    try:
        target_id = owner_id(tenant)
    except ProfileSettingsError:
        return None  # Optional metadata must not break legacy login/profile reads.
    if owner.id != target_id or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',str(tenant.slug)):
        return None
    values = profile_values(tenant,owner)
    try:
        json.dumps(values, allow_nan=False)
        revision = profile_revision(tenant, owner, values)
    except (TypeError, ValueError):
        return None
    access = profile_editability(can_edit=can_edit, writes_blocked=writes_blocked)
    return {'contract_version':CONTRACT,'tenant':{'id':tenant.id,'slug':tenant.slug},
        'revision':revision,'values':values,
        'can_edit':access['mode']=='editable','editability':access,'save_endpoint':f'/api/admin/tenants/{tenant.slug}/config',
        'ui': {**organization_type_descriptor(getattr(tenant, 'tipo', None)),
            'activity_label': 'Rubro o actividad',
            'activity_description': 'Describe la actividad de la organización. Las funciones y los permisos se administran por separado.'},
        'concurrency':'expected_revision','provider_calls_performed':False}


def validate_public_url(value):
    if not value: return
    try:
        url=urlsplit(value)
        host=(url.hostname or '').lower().rstrip('.')
        if (url.scheme not in ('https','http') or not host or url.username or url.password
                or url.port not in (None,80,443) or '.' not in host or '\\' in value
                or host.endswith(('.local','.internal','.localhost','.invalid'))):
            raise ValueError()
        try: address=ipaddress.ip_address(host)
        except ValueError: address=None
        if address is not None and not address.is_global: raise ValueError()
    except ValueError:
        raise ProfileSettingsError('profile_url_invalid',message='Ingresá una dirección web pública sin usuario ni contraseña.') from None


def validate_changes(changes, current):
    if not isinstance(changes,dict) or not changes or set(changes)-FIELDS:
        raise ProfileSettingsError('profile_fields_invalid')
    result={}
    for key,value in changes.items():
        if key == RECEIPT_HEADER_KEY:
            value = validate_receipt_header(value)
        elif key in LIMITS:
            if not isinstance(value,str) or len(value)>LIMITS[key] or any(ord(c)<32 for c in value):
                raise ProfileSettingsError('profile_text_invalid',message=f'Revisá el formato de {key}.')
            value=value.strip()
            if key=='nombre_empresa' and not value: raise ProfileSettingsError('profile_name_required')
            if key in ('link_web','logo_url'): validate_public_url(value)
        elif key in ('latitud','longitud'):
            limit=90 if key=='latitud' else 180
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float))
                    or not math.isfinite(value) or abs(value)>limit):
                raise ProfileSettingsError('profile_coordinates_invalid')
        elif key=='horario_json':
            if not isinstance(value,list) or len(value) not in (0,7):
                raise ProfileSettingsError('profile_hours_invalid')
            for index,entry in enumerate(value):
                if not isinstance(entry,dict) or set(entry)!={'dia','abre','cierra','cerrado'}:
                    raise ProfileSettingsError('profile_hours_invalid')
                if entry['dia']!=DAYS[index] or type(entry['cerrado']) is not bool:
                    raise ProfileSettingsError('profile_hours_invalid')
                if entry['cerrado']:
                    if entry['abre']!='' or entry['cierra']!='': raise ProfileSettingsError('profile_hours_invalid')
                elif any(not isinstance(entry[k],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',entry[k]) for k in ('abre','cierra')):
                    raise ProfileSettingsError('profile_hours_invalid')
            value=deepcopy(value)
        result[key]=value
    merged={**current,**result}
    if (merged['latitud'] is None)!=(merged['longitud'] is None):
        raise ProfileSettingsError('profile_coordinates_pair_required')
    return result

def save_profile_settings(session, tenant_model, user_model, audit_model, *, tenant_id, actor_id, data, authorize):
    try:
        if not isinstance(data,dict) or set(data)-{'organization_profile','expected_revision'}:
            raise ProfileSettingsError('profile_request_invalid')
        expected=data.get('expected_revision')
        if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):
            raise ProfileSettingsError('profile_revision_required',428,'Actualizá el perfil antes de guardar.')
        if session.get_bind().dialect.name=='postgresql':
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
        tenant=session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update(of=tenant_model).one_or_none()
        if tenant is None or tenant.is_active is not True: raise ProfileSettingsError('tenant_unavailable',403)
        target_id=owner_id(tenant)
        # User eagerly joins optional ticket categories. PostgreSQL cannot lock
        # their nullable outer-join side; only the actor/owner rows need locks.
        people=session.query(user_model).filter(user_model.id.in_({actor_id,target_id})).order_by(user_model.id).populate_existing().with_for_update(of=user_model).all()
        people={person.id:person for person in people}
        actor,owner=people.get(actor_id),people.get(target_id)
        if actor is None or owner is None or not authorize(actor,tenant):
            raise ProfileSettingsError('profile_update_forbidden',403,'No tenés permiso para modificar esta organización.')
        other=session.query(tenant_model.id).filter(tenant_model.id!=tenant.id,
            or_(tenant_model.municipio_id==target_id,tenant_model.pyme_id==target_id)).first()
        if other is not None: raise ProfileSettingsError('profile_owner_shared',409)
        before=build_profile_settings(tenant,owner,can_edit=True)
        if before is None: raise ProfileSettingsError('profile_identity_invalid',409)
        if before['revision']!=expected:
            raise ProfileSettingsError('profile_revision_conflict',412,
                'Otra persona actualizó este perfil. Conservamos tu edición: revisá la versión actual antes de volver a guardar.')
        changes=validate_changes(data.get('organization_profile'),before['values'])
        if RECEIPT_HEADER_KEY in changes and tenant.configuracion is not None and not isinstance(tenant.configuracion, dict):
            raise ProfileSettingsError('profile_legacy_config_invalid',409)
        changed={key:value for key,value in changes.items() if value!=before['values'][key]}
        if RECEIPT_HEADER_KEY in changes:
            config = tenant.configuracion or {}
            value = changes[RECEIPT_HEADER_KEY]
            if ((value is None and RECEIPT_HEADER_KEY in config)
                    or (value is not None and config.get(RECEIPT_HEADER_KEY) != value)):
                changed[RECEIPT_HEADER_KEY] = value
        for key,value in changed.items():
            if key == RECEIPT_HEADER_KEY:
                config = deepcopy(tenant.configuracion or {})
                if value is None:
                    config.pop(RECEIPT_HEADER_KEY, None)
                else:
                    config[RECEIPT_HEADER_KEY] = deepcopy(value)
                tenant.configuracion = config
            elif key=='horario_json' or key in CONFIG_TEXT_FIELDS:
                if tenant.configuracion is not None and not isinstance(tenant.configuracion,dict):
                    raise ProfileSettingsError('profile_legacy_config_invalid',409)
                config_key = HOURS_KEY if key == 'horario_json' else CONFIG_TEXT_FIELDS[key]
                tenant.configuracion={**(tenant.configuracion or {}),config_key:value}
            else:
                setattr(owner,key,value)
                if key=='nombre_empresa': tenant.nombre=value
                if key=='logo_url': tenant.logo_url=value
        session.flush()
        after=build_profile_settings(tenant,owner,can_edit=True)
        if changed:
            session.add(audit_model(tenant_id=tenant.id,actor_user_id=actor.id,
                event_type='organization.profile.updated',resource_type='tenant_profile',resource_id=str(tenant.id),
                details={'contract_version':CONTRACT,'changed_fields':sorted(changed),
                    'previous_revision':before['revision'],'revision':after['revision'],'provider_calls_performed':False}))
        result={'contract_version':'organization.profile_save.v1','ok':True,'saved':bool(changed),
            'tenant':after['tenant'],'profile':after,'provider_calls_performed':False}
        json.dumps(result,allow_nan=False)
        session.commit()
        return result
    except SQLAlchemyError as exc:
        session.rollback()
        raise ProfileSettingsError('profile_save_unconfirmed',503,
            'No pudimos confirmar el guardado. Verificá el perfil antes de reintentar.') from exc
    except BaseException:
        session.rollback()
        raise
