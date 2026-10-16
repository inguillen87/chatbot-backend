"""Opt-in private access to an existing evaluation guide, never provisioning."""
from copy import deepcopy
import re

from services.accessible_support_guide import GUIDE_SHA256, load_guide, menu_response

CONFIG_KEY = 'private_conversation_guide'
GUIDE_ID = 'accessible-support-evaluation'
ACCESS_CONTRACT = 'tenant.conversation_guide_access.v1'
CONTRACT = 'tenant.conversation_guide.v1'
UI = {
    'heading': 'Guía de atención accesible',
    'description': 'Recorrido privado de evaluación basado en el plan de IA accesible y Mesa Única. No crea trámites ni consulta registros oficiales. Usá ejemplos sin datos personales.',
    'open': 'Explorar la guía',
    'start': 'Volver al inicio',
    'back_to_menu': 'Volver al menú',
    'source_label': 'Fuente del contenido',
    'loading': 'Cargando la guía de esta organización…',
    'error': 'No pudimos cargar este paso. Volvé a intentar; no se creó ningún trámite.',
}


def guide_access_descriptor(tenant, *, can_read=False):
    """No content reads or writes; callers supply verified control-plane access."""
    if can_read is not True or getattr(tenant, 'is_active', False) is not True:
        return None
    tenant_id, slug = getattr(tenant, 'id', None), getattr(tenant, 'slug', None)
    if type(tenant_id) is not int or tenant_id <= 0 or not isinstance(slug, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}', slug):
        return None
    config = getattr(tenant, 'configuracion', None)
    enabled = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    if (not isinstance(enabled, dict) or set(enabled) != {'enabled', 'guide_id'}
            or enabled.get('enabled') is not True or enabled.get('guide_id') != GUIDE_ID):
        return None
    return {
        'contract_version': ACCESS_CONTRACT,
        'tenant': {'id': tenant_id, 'slug': slug},
        'guide_id': GUIDE_ID,
        'evaluation_only': True,
        'endpoint': f'/api/admin/tenants/{slug}/conversation-guide',
        'ui': deepcopy(UI),
    }


def guide_menu_payload(descriptor, *, node='start', selection=None):
    guide = load_guide()
    return {
        'contract_version': CONTRACT,
        'tenant': deepcopy(descriptor['tenant']),
        'guide_id': GUIDE_ID,
        'evaluation_only': True,
        'guide_sha256': GUIDE_SHA256,
        'source': deepcopy(guide['source']),
        'policy': deepcopy(guide['policy']),
        'menu': menu_response(node, selection, guide=guide),
        'ui': deepcopy(UI),
        'writes_performed': False,
        'provider_calls_performed': False,
    }
