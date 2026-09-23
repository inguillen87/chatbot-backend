"""Scoped presentation for the existing organization profile, not authorization.

No provisioning, credential inspection, provider calls or entitlement mutation.
The authenticated profile route supplies the already resolved tenant.
"""
import re

CONTRACT_VERSION = 'organization.profile_workspace.v1'
_TYPES = {
    'municipio': ('municipio', 'Municipio', 'Perfil del municipio'),
    'gobierno': ('gobierno', 'Gobierno', 'Perfil del gobierno'),
    'government': ('gobierno', 'Gobierno', 'Perfil del gobierno'),
    'colegio': ('colegio', 'Colegio', 'Perfil del colegio'),
    'school': ('colegio', 'Colegio', 'Perfil del colegio'),
    'empresa': ('empresa', 'Empresa', 'Perfil de la empresa'),
    'pyme': ('pyme', 'Pyme', 'Perfil de la pyme'),
}
_SECTIONS = (
    ('general', 'Datos de la organización', 'Nombre, contacto y sitio informativo.'),
    ('identity', 'Marca e identidad', 'Logo e imagen que identifican este espacio.'),
    ('location', 'Ubicación', 'Domicilio y coordenadas de atención.'),
    ('hours', 'Horarios', 'Disponibilidad del equipo para atender.'),
    ('channels', 'Canales e integraciones', 'Revisá WhatsApp y las conexiones del espacio actual.'),
    ('plan-security', 'Plan, equipo y seguridad', 'Consultá consumo y permisos sin cambiar de organización.'),
)


def build_organization_workspace(tenant):
    tenant_id = getattr(tenant, 'id', None)
    slug = getattr(tenant, 'slug', None)
    if (isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id < 1
            or not isinstance(slug, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}', slug)
            or getattr(tenant, 'is_active', False) is not True):
        return None
    raw_kind = getattr(tenant, 'tipo', None)
    kind, label, heading = _TYPES.get(
        raw_kind.strip().lower() if isinstance(raw_kind, str) else '',
        ('organizacion', 'Organización', 'Perfil de la organización'))
    sender = getattr(tenant, 'whatsapp_sender_id', None)
    has_sender = isinstance(sender, str) and bool(sender.strip())
    return {
        'contract_version': CONTRACT_VERSION,
        'tenant': {'id': tenant_id, 'slug': slug},
        'organization_type': kind, 'organization_label': label, 'heading': heading,
        'description': 'Configurá este espacio sin crear otra cuenta. Los permisos y las funciones siguen dependiendo de tu organización y plan.',
        'sections': [{'id': key, 'label': title, 'description': detail} for key, title, detail in _SECTIONS],
        'continuity': {'preserve_existing_account': True, 'whatsapp_registration_present': has_sender,
            'note': ('Ya existe un registro de WhatsApp. Revisá su estado en Integraciones antes de iniciar otra conexión.'
                     if has_sender else 'Administrá WhatsApp desde Integraciones. El teléfono de contacto no acredita una conexión.')},
        'domain_note': 'El sitio informativo del perfil no cambia el dominio de acceso ni verifica su propiedad.',
        'writes_performed': False, 'provider_calls_performed': False,
    }
