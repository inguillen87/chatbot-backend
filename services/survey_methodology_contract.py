"""Closed, backend-owned schema for declared research methodology (not certification)."""
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import re
import unicodedata

CONTRACT = 'surveys.methodology.v1'
WRITE_CONTRACT = 'surveys.methodology.write.v1'
MAX_BODY_BYTES = 32768
HISTORY_LIMIT = 10

SCHEMA = [
 {'id':'study','title':'Estudio y responsables','fields':[
  {'key':'purpose','label':'Objetivo del estudio','kind':'textarea','max_length':1000,'help':'Qu\u00e9 decisi\u00f3n o pregunta busca informar este estudio.'},
  {'key':'sponsor','label':'Qui\u00e9n encarga o financia','kind':'text','max_length':200,'help':'Organizaci\u00f3n responsable del encargo. No incluir datos de participantes.'},
  {'key':'research_team','label':'Equipo que realiza el estudio','kind':'text','max_length':240,'help':'Identificaci\u00f3n del equipo o consultora responsable.'},
  {'key':'population','label':'Universo de inter\u00e9s','kind':'textarea','max_length':1000,'help':'A qui\u00e9nes se refiere el estudio; delimitar territorio y criterios de inclusi\u00f3n.'}]},
 {'id':'sample','title':'Marco y reclutamiento','fields':[
  {'key':'design','label':'Dise\u00f1o declarado','kind':'select','max_length':32,'help':'Es una declaraci\u00f3n del responsable, no una validaci\u00f3n estad\u00edstica.', 'options':[
    {'value':'unknown','label':'No documentado'}, {'value':'probability','label':'Probabil\u00edstico declarado'},
    {'value':'nonprobability','label':'No probabil\u00edstico / participaci\u00f3n abierta'}, {'value':'census','label':'Relevamiento censal declarado'},
    {'value':'mixed','label':'Mixto declarado'}, {'value':'synthetic','label':'Datos sint\u00e9ticos / demostraci\u00f3n'}]},
  {'key':'sampling_frame','label':'Marco o fuente de selecci\u00f3n','kind':'textarea','max_length':1200,'help':'Describir la fuente o listado y su cobertura. No adjuntar datos individuales.'},
  {'key':'recruitment','label':'C\u00f3mo se convoc\u00f3 o seleccion\u00f3','kind':'textarea','max_length':1200,'help':'Procedimiento de selecci\u00f3n, contacto o acceso abierto; exclusiones relevantes.'},
  {'key':'incentives','label':'Incentivos','kind':'text','max_length':500,'help':'Indicar incentivos o declarar expl\u00edcitamente que no se utilizaron.'}]},
 {'id':'fieldwork','title':'Trabajo de campo declarado','fields':[
  {'key':'collection_modes','label':'Canales de recolecci\u00f3n','kind':'text','max_length':250,'help':'Por ejemplo: web, WhatsApp, tel\u00e9fono o entrevista presencial. No se infiere desde el tr\u00e1fico.'},
  {'key':'languages','label':'Idiomas ofrecidos','kind':'text','max_length':180,'help':'Idiomas efectivamente ofrecidos o previstos, aclarando la condici\u00f3n.'},
  {'key':'fieldwork_start','label':'Inicio de campo declarado','kind':'date','max_length':10,'help':'Fecha declarada; no cambia la apertura de la encuesta.'},
  {'key':'fieldwork_end','label':'Fin de campo declarado','kind':'date','max_length':10,'help':'Fecha declarada; no cierra la participaci\u00f3n.'}]},
 {'id':'analysis','title':'Procesamiento y l\u00edmites','fields':[
  {'key':'quality_controls','label':'Controles de calidad','kind':'textarea','max_length':1200,'help':'Procedimientos usados o previstos y criterios de revisi\u00f3n. No ejecuta exclusiones.'},
  {'key':'weighting','label':'Ponderaci\u00f3n y ajustes','kind':'textarea','max_length':1200,'help':'Describir pesos y ajustes o declarar ausencia de ponderaci\u00f3n. No los aplica al tablero.'},
  {'key':'limitations','label':'Limitaciones conocidas','kind':'textarea','max_length':1200,'help':'Cobertura, autoselecci\u00f3n, no respuesta y otras restricciones de interpretaci\u00f3n.'},
  {'key':'instrument_notes','label':'Notas del cuestionario','kind':'textarea','max_length':1000,'help':'Referencias y observaciones. La revisi\u00f3n del instrumento se vincula autom\u00e1ticamente al guardar.'}]}]
DEFINITIONS = {f['key']: f for group in SCHEMA for f in group['fields']}
UI = {
 'title':'Ficha metodol\u00f3gica del estudio', 'eyebrow':'Investigaci\u00f3n y consultor\u00eda',
 'description':'Document\u00e1 c\u00f3mo se obtuvo y analiz\u00f3 la informaci\u00f3n. Cada guardado conserva una versi\u00f3n y su motivo.',
 'disclaimer':'Declaraci\u00f3n del responsable: completar esta ficha no certifica representatividad, dise\u00f1o ni margen de error. No modifica respuestas ni resultados.',
 'coverage':'Campos documentados', 'missing':'Pendiente de documentar', 'documented':'Declaraci\u00f3n registrada',
 'revision':'Versi\u00f3n de ficha', 'instrument_revision':'Revisi\u00f3n del cuestionario',
 'stale':'El cuestionario cambi\u00f3 desde esta ficha. Revis\u00e1 la declaraci\u00f3n antes de guardar una versi\u00f3n vinculada al instrumento actual.',
 'edit':'Editar declaraci\u00f3n', 'save':'Guardar nueva versi\u00f3n', 'saving':'Guardando\u2026', 'saved':'Versi\u00f3n guardada y auditada.',
 'unchanged':'No hay cambios que registrar.', 'change_reason':'Motivo del cambio',
 'change_help':'Explic\u00e1 qu\u00e9 document\u00e1s o correg\u00eds (entre 8 y 500 caracteres).',
 'author':'Usuario que registró', 'history':'Historial reciente', 'history_more':'Se muestran las diez versiones m\u00e1s recientes.',
 'view_revision':'Ver versi\u00f3n', 'current':'Ver ficha actual', 'viewing_history':'Est\u00e1s consultando una versi\u00f3n anterior, de solo lectura.',
 'discard':'Descartar cambios y recargar', 'discard_title':'\u00bfDescartar la edici\u00f3n local?',
 'discard_description':'Los cambios no guardados se perder\u00e1n. Las versiones ya registradas permanecen intactas.',
 'cancel':'Seguir editando', 'confirm_discard':'Descartar y recargar',
 'refresh':'Actualizar ficha', 'refresh_failed':'No se pudo verificar la ficha actual. No guardes desde datos desactualizados.',
 'conflict':'Hay una versi\u00f3n o cuestionario m\u00e1s reciente. Tu edici\u00f3n se conserva; revis\u00e1 antes de reemplazarla.',
 'pending':'Cambios sin guardar', 'archive':'El instrumento est\u00e1 archivado. La ficha se conserva como lectura.',
 'disabled':'La ficha metodol\u00f3gica a\u00fan no est\u00e1 habilitada en este entorno.',
 'read_error':'No pudimos cargar una ficha verificable.',
 'write_error':'No pudimos confirmar el guardado. Tu edici\u00f3n se conserva; actualiz\u00e1 antes de reintentar.',
}


class MethodologyInputError(ValueError):
    def __init__(self, code, message, field=None):
        super().__init__(message); self.code=code; self.field=field


def clean_text(value, maximum, *, field, allow_empty=True):
    if not isinstance(value,str):
        raise MethodologyInputError('methodology_field_invalid','El valor debe ser texto.',field)
    value=unicodedata.normalize('NFC',value.replace('\r\n','\n').replace('\r','\n')).strip()
    if len(value)>maximum or (not value and not allow_empty):
        raise MethodologyInputError('methodology_field_length','La longitud del campo no es v\u00e1lida.',field)
    if any(unicodedata.category(c) in ('Cc','Cf','Cs') and c not in ('\n','\t') for c in value):
        raise MethodologyInputError('methodology_field_control_characters','El texto contiene caracteres de control no admitidos.',field)
    return value


def blank_fields():
    return {key:('unknown' if key=='design' else '') for key in DEFINITIONS}


def validate_fields(value):
    if not isinstance(value,dict) or set(value)!=set(DEFINITIONS):
        raise MethodologyInputError('methodology_fields_invalid','La ficha debe contener exactamente los campos de su versi\u00f3n.')
    fields={}
    for key,definition in DEFINITIONS.items():
        field=clean_text(value[key],definition['max_length'],field=key)
        if definition['kind']=='select' and field not in {o['value'] for o in definition['options']}:
            raise MethodologyInputError('methodology_design_invalid','Seleccion\u00e1 un dise\u00f1o declarado v\u00e1lido.',key)
        if definition['kind']=='date' and field:
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',field) or date.fromisoformat(field).isoformat()!=field:
                    raise ValueError()
            except ValueError:
                raise MethodologyInputError('methodology_date_invalid','La fecha no es v\u00e1lida.',key) from None
        fields[key]=field
    if fields['fieldwork_start'] and fields['fieldwork_end'] and fields['fieldwork_end']<fields['fieldwork_start']:
        raise MethodologyInputError('methodology_dates_reversed','La fecha final no puede ser anterior al inicio.','fieldwork_end')
    return fields


def validate_write(value):
    expected={'contract_version','expected_revision','expected_instrument_revision','fields','change_reason'}
    if not isinstance(value,dict) or set(value)!=expected or value['contract_version']!=WRITE_CONTRACT:
        raise MethodologyInputError('methodology_write_invalid','La solicitud no coincide con el contrato metodol\u00f3gico.')
    for key,minimum in [('expected_revision',0),('expected_instrument_revision',1)]:
        if type(value[key]) is not int or not minimum<=value[key]<2147483647:
            raise MethodologyInputError('methodology_revision_invalid','La revisi\u00f3n esperada no es v\u00e1lida.',key)
    reason=clean_text(value['change_reason'],500,field='change_reason',allow_empty=False)
    if len(reason)<8:
        raise MethodologyInputError('methodology_reason_required','Describ\u00ed el motivo con al menos 8 caracteres.','change_reason')
    return {**value,'fields':validate_fields(value['fields']),'change_reason':reason}


def canonical_digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def coverage(fields):
    missing=[key for key,value in fields.items() if not value or key=='design' and value=='unknown']
    return {'documented':len(DEFINITIONS)-len(missing),'total':len(DEFINITIONS),'missing':missing,
            'assessment':'self_declared','inference_authorized':False}


def schema():
    return deepcopy(SCHEMA)
