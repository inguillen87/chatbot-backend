"""Strong tenant control-plane authorization helpers.

Operational tenant membership is intentionally broader than control-plane
administration: employees may read or work tickets, but only an explicit
tenant administrator (or allowlisted platform superadmin) may mutate branding,
channel configuration, or provider sender assignments.
"""

from __future__ import annotations

from models import Role, TenantProfile, User, UserRole
from utils.roles import (
    PERM_MANAGE_CATALOG,
    ROLE_TENANT_ADMIN,
    canonical_role,
    has_permission,
    is_authorized_superadmin_user,
)


def _belongs_to_tenant(user: User, tenant: TenantProfile) -> bool:
    if getattr(user, "tenant_id", None) == tenant.id:
        return True

    user_slug = str(getattr(user, "tenant_slug", None) or "").strip().lower()
    tenant_slug = str(getattr(tenant, "slug", None) or "").strip().lower()
    if user_slug and tenant_slug and user_slug == tenant_slug:
        return True

    user_id = getattr(user, "id", None)
    owner_ids = {
        owner_id
        for owner_id in (
            getattr(tenant, "municipio_id", None),
            getattr(tenant, "pyme_id", None),
        )
        if owner_id is not None
    }
    if user_id in owner_ids:
        return True

    return any(
        getattr(user, field, None) in owner_ids
        for field in ("municipio_id", "pyme_id", "empresa_id")
    )


def _has_scoped_tenant_admin_role(user: User, tenant: TenantProfile) -> bool:
    assignments = (
        UserRole.query.join(Role, UserRole.role_id == Role.id)
        .filter(
            UserRole.user_id == user.id,
            UserRole.tenant_id == tenant.id,
        )
        .all()
    )
    return any(
        canonical_role(getattr(assignment.role, "name", None)) == ROLE_TENANT_ADMIN
        for assignment in assignments
    )


def _has_scoped_permission(user: User, tenant: TenantProfile, permission: str) -> bool:
    assignments = (
        UserRole.query.join(Role, UserRole.role_id == Role.id)
        .filter(
            UserRole.user_id == user.id,
            UserRole.tenant_id == tenant.id,
        )
        .all()
    )
    return any(
        has_permission(getattr(assignment.role, "name", None), permission)
        for assignment in assignments
    )


def can_manage_tenant_control_plane(
    user: User | None,
    tenant: TenantProfile | None,
    *,
    require_active: bool = True,
) -> bool:
    """Authorize high-impact tenant configuration mutations.

    A main tenant-admin role or an explicit tenant-scoped admin role is
    required in addition to membership. Platform superadmins remain allowed.
    """

    if user is None or tenant is None:
        return False
    if require_active and getattr(tenant, "is_active", True) is not True:
        return False
    if is_authorized_superadmin_user(user):
        return True
    if not _belongs_to_tenant(user, tenant):
        return False
    if canonical_role(getattr(user, "rol", None)) == ROLE_TENANT_ADMIN:
        return True
    return _has_scoped_tenant_admin_role(user, tenant)


def can_manage_tenant_catalog(
    user: User | None,
    tenant: TenantProfile | None,
    *,
    require_active: bool = True,
) -> bool:
    """Authorize catalog operations within exactly one active tenant."""

    if user is None or tenant is None:
        return False
    if require_active and getattr(tenant, "is_active", True) is not True:
        return False
    if is_authorized_superadmin_user(user):
        return True
    if not _belongs_to_tenant(user, tenant):
        return False
    if has_permission(getattr(user, "rol", None), PERM_MANAGE_CATALOG):
        return True
    return _has_scoped_permission(user, tenant, PERM_MANAGE_CATALOG)
