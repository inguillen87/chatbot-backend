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
