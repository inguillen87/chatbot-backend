"""Read-only orientation for an existing tenant's WhatsApp setup.

This is presentation metadata, not plan entitlement or provider verification.
No tenant, account, sender, credential, module or domain is created or changed.
"""
from collections.abc import Mapping
import re
from urllib.parse import quote

CONTRACT_VERSION = 'whatsapp.self_service.v1'
LABELS = {'municipio': 'Municipio', 'gobierno': 'Gobierno', 'government': 'Gobierno',
          'colegio': 'Colegio', 'school': 'Colegio', 'empresa': 'Empresa', 'pyme': 'Pyme'}
PROFILE_LABELS = {'municipio': 'Perfil del municipio', 'gobierno': 'Perfil institucional',
                  'government': 'Perfil institucional', 'colegio': 'Perfil del colegio',
                  'school': 'Perfil del colegio', 'empresa': 'Perfil de la empresa', 'pyme': 'Perfil de la pyme'}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def build_whatsapp_self_service(tenant, state):
    slug = _text(getattr(tenant, 'slug', None))
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}', slug):
        return None
    state = state if isinstance(state, Mapping) else {}
    kind = _text(getattr(tenant, 'tipo', None)).lower()
    existing = any(_text(v) for v in (state.get('sender_id'), state.get('sender_sid'),
                                      getattr(tenant, 'whatsapp_sender_id', None)))
    prefix = '/t/' + quote(slug, safe='')
    sections = [
        ('profile', PROFILE_LABELS.get(kind, 'Perfil de la organización'), '/perfil',
         'Revisá la identidad y los datos del espacio actual.'),
        ('implementation', 'Módulos y equipo', '/implementacion',
         'Continuá los pasos disponibles según el plan y los permisos de tu equipo.'),
        ('integrations', 'Integraciones', '/integracion',
         'Administrá los canales de esta organización sin crear otro espacio.'),
        ('templates', 'Mensajes y plantillas', '/perfil/plantillas-respuesta',
         'Prepará las respuestas y consultá su aprobación antes de utilizarlas.'),
    ]
    return {
        'contract_version': CONTRACT_VERSION,
        'tenant': {'id': getattr(tenant, 'id', None), 'slug': slug},
        'organization_label': LABELS.get(kind, 'Organización'),
        'mode': 'existing_connection' if existing else 'new_connection',
        'heading': 'Continuá con tu conexión existente' if existing else 'Configurá WhatsApp en tu organización',
        'description': ('Conservá la cuenta y el número registrados. Revisá su estado y completá lo que falta; no necesitás iniciar otra alta.'
                        if existing else 'Completá el perfil, autorizá el canal y revisá los mensajes en este mismo espacio.'),
        'existing_connection': existing,
        'provider_calls_performed': False,
        'writes_performed': False,
        'permissions_note': 'Cada pantalla valida tu plan y tus permisos. Un enlace no habilita funciones adicionales.',
        'verification_note': 'Tener un número registrado no confirma la entrega de mensajes ni la aprobación de plantillas.',
        'sections': [{'id': key, 'label': label, 'href': prefix+path, 'description': detail}
                     for key, label, path, detail in sections],
    }
