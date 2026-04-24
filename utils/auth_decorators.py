"""Lightweight authentication decorators for API endpoints."""

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

from flask import abort, g
from utils.roles import has_permission, ROLE_SUPERADMIN

F = TypeVar("F", bound=Callable[..., object])


def require_auth(func: F) -> F:
    """Ensure a viewer is attached to the request context."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        viewer = getattr(g, "viewer", None)
        if viewer is None:
            abort(401, description="Autenticación requerida")
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def require_auth_optional(func: F) -> F:
    """Invoke the wrapped function regardless of authentication state."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]

def require_role(role: str) -> Callable[[F], F]:
    """Ensure the user has a specific role."""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            viewer = getattr(g, "viewer", None)
            if viewer is None:
                abort(401, description="Autenticación requerida")

            if viewer.rol != role and viewer.rol != ROLE_SUPERADMIN:
                abort(403, description=f"Rol requerido: {role}")

            return func(*args, **kwargs)
        return wrapper
    return decorator

def require_permission(permission: str) -> Callable[[F], F]:
    """Ensure the user has a specific permission based on RBAC."""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            viewer = getattr(g, "viewer", None)
            if viewer is None:
                abort(401, description="Autenticación requerida")

            if not has_permission(viewer.rol, permission):
                abort(403, description=f"Permiso requerido: {permission}")

            return func(*args, **kwargs)
        return wrapper
    return decorator

def _is_authorized_for_tenant(
    user, tenant_id: int | None = None, tenant_slug: str | None = None
) -> bool:
    """
    Checks if a user is authorized for a given tenant.
    Authorization rules:
    - Platform admins ('super_admin', 'admin') are always authorized.
    - Users are authorized if their `tenant_id` matches.
    - Users are authorized if they are the owner of the tenant (`municipio_id` or `pyme_id`).
    - Users (like employees) are authorized if their `empresa_id` links to the tenant's owner.
    """
    if not user:
        return False

    # Platform-level admins are authorized for any tenant
    if user.rol in ("super_admin", "admin"):
        return True

    # Direct tenant membership
    if tenant_id is not None and getattr(user, "tenant_id", None) == tenant_id:
        return True

    # Resolve tenant to check ownership if tenant_id or tenant_slug is provided
    from models import TenantProfile  # Local import to avoid circular dependencies

    tenant = None
    if tenant_id:
        tenant = TenantProfile.query.get(tenant_id)
    elif tenant_slug:
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()

    if not tenant:
        # Cannot determine authorization without a tenant context
        return False

    # Re-check direct membership with the resolved tenant object
    if getattr(user, "tenant_id", None) == tenant.id:
        return True

    # Direct ownership
    if tenant.municipio_id == user.id or tenant.pyme_id == user.id:
        return True

    # Organizational affiliation (e.g., employee of the owner)
    if user.empresa_id:
        if tenant.municipio_id == user.empresa_id or tenant.pyme_id == user.empresa_id:
            return True

    return False
