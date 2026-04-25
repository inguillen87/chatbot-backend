from models import TenantProfile

def is_education_tenant(tenant_id: int) -> bool:
    """
    Checks if a given tenant has the vertical flag set to 'educacion'.
    Used to toggle capabilities or enforce specific strict policies.
    """
    if not tenant_id:
        return False

    tenant = TenantProfile.query.get(tenant_id)
    if not tenant:
        return False

    return tenant.vertical == 'educacion'
