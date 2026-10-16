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


def resolve_consistent_user_tenant(user: User | None) -> TenantProfile | None:
    """Require one consistent organization, including legacy owner references.

    Matching one field cannot override a different explicit membership or
    owner. Reuse the authentication resolver so /me and direct private routes
    agree, without accepting public request selectors as membership.
    """
    from utils.auth_helpers import auth_tenant_for_user

    if user is None:
        return None

    resolved = auth_tenant_for_user(user)
    legacy_owner_ids = {
        owner_id for owner_id in (
            getattr(user, "municipio_id", None), getattr(user, "pyme_id", None)
        ) if owner_id is not None
    }
    if resolved is None:
        # An unresolved explicit association is a conflict, not permission to
        # fall back to another legacy reference. Legacy-only employees remain
        # compatible only when their owner resolves to exactly one tenant.
        if any(getattr(user, field, None) for field in ("tenant_id", "tenant_slug", "empresa_id")):
            return None
        if not legacy_owner_ids:
            return None
        if TenantProfile.query.filter(
            (TenantProfile.municipio_id == user.id) | (TenantProfile.pyme_id == user.id)
        ).first() is not None:
            # The central resolver already rejected ambiguous ownership. An
            # unrelated legacy owner must not resolve that ambiguity.
            return None
        candidates = TenantProfile.query.filter(
            (TenantProfile.municipio_id.in_(legacy_owner_ids))
            | (TenantProfile.pyme_id.in_(legacy_owner_ids))
        ).order_by(TenantProfile.id.asc()).limit(2).all()
        if len(candidates) != 1:
            return None
        resolved = candidates[0]

    resolved_owner_ids = {
        owner_id for owner_id in (resolved.municipio_id, resolved.pyme_id)
        if owner_id is not None
    }
    return resolved if legacy_owner_ids.issubset(resolved_owner_ids) else None


def _belongs_to_tenant(user: User, tenant: TenantProfile) -> bool:
    resolved = resolve_consistent_user_tenant(user)
    return resolved is not None and resolved.id == tenant.id


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
