"""Lightweight authentication decorators for API endpoints."""

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

from flask import abort, g


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
