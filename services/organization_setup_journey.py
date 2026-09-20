from services.organization_modules import apply_setup_selection, ModuleSelectionError
"""Read-only, tenant-bound setup guidance. No provisioning or entitlement changes.

The legacy government journey remains available. This optional v2 projection
uses only published channel evidence; completing it never authorizes production.
"""
from copy import deepcopy
from collections import Counter
from urllib.parse import parse_qs, unquote, urlsplit
from services.organization_workspace import build_organization_workspace
from services.tenant_implementation_journey import _stage, _STAGES

CONTRACT_VERSION = 'tenant.implementation_journey.v2'
_COMMERCIAL = frozenset({'empresa', 'pyme', 'colegio'})
_LABELS = {
    'municipio': ('Puesta en marcha del municipio', 'Trámites, atención ciudadana y seguimiento con responsables.'),
    'gobierno': ('Puesta en marcha del gobierno', 'Servicios públicos y atención institucional en el mismo espacio.'),
    'colegio': ('Puesta en marcha del colegio', 'Servicios educativos, atención a familias y cobros según las funciones contratadas.'),
    'empresa': ('Puesta en marcha de la empresa', 'Catálogo, consultas, pedidos y cobros con seguimiento del equipo.'),
    'pyme': ('Puesta en marcha de la pyme', 'Atención y ventas sobre una configuración compartida, sin duplicar cuentas.'),
    'organizacion': ('Puesta en marcha de la organización', 'Configuración general sin asumir un sector que no fue confirmado.'),
}


def _safe_action(action, slug):
    if not isinstance(action, dict): return None
    href = action.get('href')
    if not isinstance(href, str) or len(href) > 1800: return None
    decoded = unquote(href)
    if not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded or any(ord(c)<32 for c in decoded): return None
    try:
        url = urlsplit(href)
        if url.scheme or url.netloc or unquote(url.path).startswith('/api/'): return None
        scopes = parse_qs(url.query, keep_blank_values=True)
        if any(v.lower()!=slug.lower() for key in ('tenant','tenant_slug','endpoint') for v in scopes.get(key, [])): return None
        parts = unquote(url.path).split('/')
        if len(parts)>2 and parts[1] in ('t','e') and parts[2].lower()!=slug.lower(): return None
        return {key:action[key] for key in ('id','label','href','kind','primary')}
    except (ValueError, KeyError): return None


def build_organization_setup_journey(tenant, channels, *, workspace_appearance=None):
    workspace = build_organization_workspace(tenant)
    if workspace is None: return None
    kind = workspace['organization_type']; slug = workspace['tenant']['slug']
    definitions = deepcopy(_STAGES)
    for definition in definitions:
        if definition['id']=='institutional_identity':
            definition['label']='Identidad del espacio'
            definition['description']='Revisá nombre y marca sin crear otra cuenta. El dominio propio y la marca blanca requieren validación y derechos de tu plan.'
        if definition['id']=='channels':
            definition['label']='Canales y atención'
            definition['description']='Revisá las conexiones existentes antes de agregar otra. Un número registrado no acredita recepción ni entrega de mensajes.'
        if definition['id']=='knowledge' and kind in _COMMERCIAL:
            definition['source_ids']=('knowledge_content','catalog_marketplace','payments_checkout')
            definition['label']='Servicios y cuotas' if kind=='colegio' else 'Catálogo, contenido y cobros'
            definition['description']='Comprobá los contenidos y cobros del espacio. Una conexión o un borrador no confirma una operación real.'
        if definition['id']=='validation_release' and kind not in ('municipio','gobierno'):
            definition['source_ids']=('crm','identity_auth','accessibility','public_intake_security')
            definition['description']='Revisá atención, acceso, accesibilidad y protección de las consultas. La aceptación productiva se valida por separado.'
    try:
        definitions = apply_setup_selection(tenant, definitions)
    except ModuleSelectionError:
        return None
    channels = [item for item in channels if isinstance(item, dict)]
    counts = Counter(str(item.get('id','')) for item in channels)
    # Ambiguous source IDs cannot become a successful last-write-wins check.
    sources = {item['id']:item for item in channels if isinstance(item.get('id'),str) and counts[item['id']]==1 and not (item.get('ready') is True and (item.get('status')!='ready' or item.get('locked') is True))}
    stages = [_stage(definition, sources) for definition in definitions]
    for stage in stages:
        stage['primary_action'] = _safe_action(stage['primary_action'], slug)
    current = next((stage for stage in stages if not stage['ready']), None)
    ready = sum(stage['ready'] for stage in stages)
    heading, description = _LABELS[kind]
    return {
        'contract_version':CONTRACT_VERSION, 'tenant':workspace['tenant'],
        'workspace_appearance':deepcopy(workspace_appearance),
        'organization_type':kind, 'organization_label':workspace['organization_label'],
        'heading':heading, 'description':description,
        'continuity_note':workspace['continuity']['note'],
        'readiness_note':'El progreso refleja estos pasos de configuración. No certifica el plan Full, la aceptación productiva ni la entrega de mensajes.',
        'government_setup':kind in ('municipio','gobierno'),
        'stages':stages,
        'summary':{'total':len(stages),'ready':ready,
            'blocked':sum(stage['status']=='blocked' for stage in stages),
            'published':sum(stage['published'] for stage in stages),
            'progress':round(ready/len(stages)*100),
            'current_stage_id':current['id'] if current else None,
            'next_action':deepcopy(current['primary_action']) if current else None},
        'writes_performed':False, 'provider_calls_performed':False,
    }
