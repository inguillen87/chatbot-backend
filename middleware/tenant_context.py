"""Resolve tenant context for public and citizen endpoints."""

from __future__ import annotations

from typing import Optional

from flask import current_app, g, jsonify, make_response, request
from werkzeug.exceptions import HTTPException
from sqlalchemy import func

from models import TenantProfile
from utils.tenant import get_current_tenant, get_current_tenant_slug, require_tenant as _decorator_require_tenant


def _normalize_slug(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    slug = value.strip().lower()
    return slug or None


def _find_tenant_by_slug(slug: str) -> Optional[TenantProfile]:
    if not slug:
        return None
    return TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower()).first()


def _find_tenant_by_widget_token(token: str) -> Optional[TenantProfile]:
    if not token:
        return None
    # Using the same logic as services/tenant_resolver.py
    try:
        return TenantProfile.query.filter(
            TenantProfile.configuracion["widget_tokens"].astext.contains(token)
        ).first()
    except Exception:
        # In case of database dialect issues (e.g. SQLite vs Postgres JSON)
        return None


def _tenant_slug_from_path(path: str | None) -> Optional[str]:
    """Return a tenant slug hinted via URL path segments.

    Supported patterns (case-insensitive):
    * /municipio/<slug>/...
    * /municipios/<slug>/...
    * /m/<slug>/...
    * /pyme/<slug>/...
    * /pymes/<slug>/...
    * /p/<slug>/...
    * /t/<slug>/... (alias used by the PWA router)
    * /api/public/<slug>/...
    * /api/pwa/public/<slug>/...
    * /api/pwa/<slug>/...
    * /api/<slug>/... (direct API aliases used by el widget/PWA)
    """

    if not path:
        return None

    segments = [segment for segment in path.split("/") if segment]
    lower_segments = [segment.lower() for segment in segments]

    # Direct API prefix where the slug comes right after `/api/<slug>/...`.
    if lower_segments[:1] == ["api"] and len(segments) >= 2:
        # Skip reserved prefixes that already have dedicated handling below
        if lower_segments[1] not in {"public", "pwa"}:
            return _normalize_slug(segments[1])

    # Direct tenant slugs prefixed at the root (e.g. /m/<slug>/...)
    if len(segments) >= 2 and lower_segments[0] in {
        "municipio",
        "municipios",
        "m",
        "pyme",
        "pymes",
        "p",
        "t",
    }:
        return _normalize_slug(segments[1])

    # Nested API prefixes where the slug is later in the path
    if lower_segments[:2] == ["api", "public"] and len(segments) >= 3:
        return _normalize_slug(segments[2])

    if lower_segments[:3] == ["api", "pwa", "public"] and len(segments) >= 4:
        return _normalize_slug(segments[3])

    if lower_segments[:2] == ["api", "pwa"] and len(segments) >= 3:
        return _normalize_slug(segments[2])

    return None


def _resolve_tenant_profile() -> Optional[TenantProfile]:
    view_args = getattr(request, "view_args", None) or {}

    slug = _normalize_slug(
        get_current_tenant_slug()
        or view_args.get("tenant_slug")
        or view_args.get("tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or request.headers.get("X-Tenant")
    )
    if slug:
        tenant = _find_tenant_by_slug(slug)
        if tenant:
            return tenant

    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    if tenant_id:
        try:
            tenant = TenantProfile.query.get(int(tenant_id))
        except (TypeError, ValueError):
            tenant = None
        if tenant:
            return tenant

    # Check for Widget Token (used by ChatWidget)
    widget_token = (
        request.headers.get("X-Widget-Token")
        or request.args.get("widget_token")
        or request.headers.get("X-Entity-Token")
        or request.args.get("entityToken")
    )
    if widget_token:
        tenant = _find_tenant_by_widget_token(widget_token)
        if tenant:
            return tenant

    # Try resolving by slug hint (query params) using the shared resolver to honor
    # widget tokens and explicit slugs coming from the web widget/marketplace.
    slug_hint = _normalize_slug(
        request.args.get("tenant_slug")
        or request.args.get("tenant")
        or view_args.get("slug")
    )
    if slug_hint:
        # First attempt direct DB lookup (fast path)
        tenant = _find_tenant_by_slug(slug_hint)
        if tenant:
            return tenant

        # Fallback to the more robust resolver which understands widget tokens
        # and explicit slug requirements.
        from services.tenant_resolver import resolve_tenant_only, TenantResolutionError

        try:
            tenant = resolve_tenant_only(
                tenant_slug=slug_hint,
                widget_token=widget_token,
                require_explicit_slug=False,
            )
            if tenant:
                return tenant
        except TenantResolutionError:
            pass

    slug = _tenant_slug_from_path(request.path)
    if slug:
        tenant = _find_tenant_by_slug(slug)
        if tenant:
            return tenant

    host = request.host.split(":", 1)[0].lower() if request.host else None
    if host:
        mapping = current_app.config.get("TENANT_DOMAIN_MAP", {})
        mapped = mapping.get(host)
        if mapped:
            tenant = _find_tenant_by_slug(mapped)
            if tenant:
                return tenant

    return None


def tenant_middleware(app) -> None:
    @app.before_request
    def attach_tenant_profile() -> None:
        tenant = _resolve_tenant_profile()
        g.tenant_profile = tenant
        g.tenant_profile_slug = tenant.slug if tenant else None
        if tenant:
            g.current_tenant = tenant
            g.current_tenant_slug = tenant.slug


def require_tenant(func=None) -> TenantProfile:
    """Ensure a tenant is resolved in the current context or abort with JSON.

    The callable can be used as a direct helper (returning the tenant or
    raising an HTTPException) or as a decorator for view functions.
    """

    if func is not None and callable(func):
        return _decorator_require_tenant(func)

    tenant = getattr(g, "tenant_profile", None) or getattr(g, "current_tenant", None)
    if tenant:
        return tenant

    tenant = get_current_tenant()
    if tenant:
        g.tenant_profile = tenant
        g.tenant_profile_slug = tenant.slug
        g.current_tenant = tenant
        g.current_tenant_slug = tenant.slug
        return tenant

    response = make_response(jsonify({"error": "tenant requerido"}), 400)
    error = HTTPException(description="tenant requerido")
    error.code = 400
    error.response = response
    raise error
