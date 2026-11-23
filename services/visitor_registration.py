"""Read-only organization resolution and declared consent for visitor signup."""

from datetime import datetime, timezone

from sqlalchemy import or_

from models import User
from services.plan_access import plan_allows_full_integrations
from services.tenant_resolver import tenants_by_exact_widget_token
from services.tenant_ticket_scope import resolve_unique_tenant_for_owner
from utils.auth_helpers import is_user_auth_disabled


def registration_tenant(owner, *, widget=False):
    if owner is None or is_user_auth_disabled(owner):
        return None
    tenant = resolve_unique_tenant_for_owner(owner.id).tenant
    if tenant is None or not tenant.is_active or owner.tenant_id not in (None, tenant.id):
        return None
    if widget and not plan_allows_full_integrations(tenant):
        return None
    return tenant


def visitor_scope_for_entity_token(token, *, require_widget_access):
    if not isinstance(token, str) or not token.strip() or token.count(".") == 2:
        return None
    token = token.strip()
    owners = User.query.filter(or_(User.token == token, User.entity_token == token)).limit(2).all()
    published_tenants = tenants_by_exact_widget_token(token)
    if len(owners) > 1 or len(published_tenants) > 1:
        return None
    if owners:
        owner = owners[0]
    elif published_tenants:
        owner = published_tenants[0].municipio or published_tenants[0].pyme
    else:
        return None
    tenant = registration_tenant(owner, widget=require_widget_access)
    if tenant is None or (published_tenants and published_tenants[0].id != tenant.id):
        return None
    return owner, tenant


def registration_consent(data):
    """HTML checkboxes and explicit JSON booleans are the signup inputs."""
    def accepted(key):
        value = data.get(key)
        return value is True or isinstance(value, str) and value.strip().lower() in {"true", "on", "1"}

    terms = accepted("acepto_terminos")
    marketing = accepted("acepta_marketing")
    now = datetime.now(timezone.utc)
    return {
        "acepto_terminos": terms,
        "fecha_aceptacion_terminos": now if terms else None,
        "acepta_marketing": marketing,
        "fecha_aceptacion_marketing": now if marketing else None,
    }
