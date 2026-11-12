"""Descriptive labels for existing tenant types, independent of access policy."""
from utils.roles import normalize_tenant_type


CONTRACT = 'organization.type_label.v1'
LABELS = {'municipio': 'Gobierno', 'colegio': 'Educación', 'pyme': 'Empresa'}


def organization_type_descriptor(value):
    """Unknown types remain unknown; this descriptor never grants access."""
    kind = normalize_tenant_type(value, default='')
    return {'organization_type_label_contract': CONTRACT,
            'organization_type_label': LABELS.get(kind)}
