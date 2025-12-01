"""Tenant utilities and decorator helpers for multitenant endpoints."""

from __future__ import annotations

from functools import wraps
from typing import Optional

from flask import g, jsonify, request
from sqlalchemy import func

from models import TenantProfile

TENANT_HEADER = "X-Tenant"


def get_current_tenant_slug() -> Optional[str]:
    """Resolve the current tenant slug from headers, query params or context."""

    slug = (
        request.headers.get(TENANT_HEADER)
        or request.args.get("tenant")
        or request.args.get("tenant_slug")
        or getattr(g, "tenant_slug", None)
        or getattr(g, "tenant_profile_slug", None)
    )

    if not slug and getattr(request, "view_args", None):
        slug = request.view_args.get("tenant_slug") or request.view_args.get("tenant")

    if slug:
        slug = str(slug).strip().lower()
        return slug or None

    return None


def get_current_tenant() -> Optional[TenantProfile]:
    """Return the tenant associated with the current request if any."""

    slug = get_current_tenant_slug()
    if not slug:
        return None

    return (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower())
        .order_by(TenantProfile.id.asc())
        .first()
    )


def _store_tenant_in_context(tenant: TenantProfile) -> None:
    g.tenant_profile = tenant
    g.tenant_profile_slug = getattr(tenant, "slug", None)
    g.current_tenant = tenant
    g.current_tenant_slug = getattr(tenant, "slug", None)


def _tenant_error_response():
    return jsonify({"error": "tenant requerido"}), 400


def require_tenant(func=None):
    """Decorator (and direct helper) ensuring a tenant exists in the request context."""

    def _ensure_tenant():
        tenant = getattr(g, "tenant_profile", None) or getattr(g, "current_tenant", None)
        if not tenant:
            tenant = get_current_tenant()

        if tenant:
            _store_tenant_in_context(tenant)
            return tenant

        return None

    if func is None:
        tenant = _ensure_tenant()
        if tenant:
            return tenant
        response, status = _tenant_error_response()
        response.status_code = status
        from werkzeug.exceptions import HTTPException

        error = HTTPException(description="tenant requerido")
        error.code = status
        error.response = response
        raise error

    @wraps(func)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return func(*args, **kwargs)

        tenant = _ensure_tenant()
        if not tenant:
            return _tenant_error_response()

        return func(*args, **kwargs)

    return wrapper

