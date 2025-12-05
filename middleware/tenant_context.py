"""Resolve tenant context for public and citizen endpoints."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from flask import current_app, g, jsonify, make_response, request
from werkzeug.exceptions import HTTPException
from sqlalchemy import func

from models import TenantProfile
from utils.tenant import (
    get_current_tenant_profile,
    get_current_tenant_slug,
    require_tenant as _decorator_require_tenant,
)


def _normalize_slug(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    slug = value.strip().lower()
    return slug or None


def _tenant_slug_from_body() -> Optional[str]:
    """Extract tenant slug hints from JSON or form payloads.

    Some widget calls send the tenant or tenant_slug inside the request body
    instead of query parameters. Reading it here allows the middleware to
    resolve a tenant without returning a 400 even when no query args are
    present.
    """

    json_payload = request.get_json(silent=True) or {}
    if isinstance(json_payload, dict):
        slug = json_payload.get("tenant_slug") or json_payload.get("tenant")
        if slug:
            return str(slug)

    form_slug = request.form.get("tenant_slug") or request.form.get("tenant")
    if form_slug:
        return str(form_slug)

    return None


def _tenant_slug_from_url(url: Optional[str]) -> Optional[str]:
    """Best-effort slug extraction from a full URL (e.g. Referer)."""

    if not url:
        return None

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    return _tenant_slug_from_path(parsed.path)


def _find_tenant_by_slug(slug: str) -> Optional[TenantProfile]:
    if not slug:
        return None
    return TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower()).first()


def _fallback_default_tenant() -> Optional[TenantProfile]:
    """Return the first available tenant as a last-resort fallback.

    This mirrors the behavior of ``resolve_tenant_only`` when
    ``require_explicit_slug`` is False, ensuring anonymous/public
    endpoints never fail with a hard 400 when at least one tenant exists.
    """
    preferred_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if preferred_slug:
        tenant = _find_tenant_by_slug(preferred_slug)
        if tenant:
            return tenant

    return TenantProfile.query.order_by(TenantProfile.id.asc()).first()


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
        # Skip reserved prefixes that map to top-level blueprints rather than
        # tenant slugs (e.g. /api/municipal/usuarios).
        if lower_segments[1] in {"public", "pwa", "municipal", "municipio"}:
            return None

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
        "market",
        "marketplace",
        "shop",
    }:
        return _normalize_slug(segments[1])

    # Nested API prefixes where the slug is later in the path
    if lower_segments[:3] == ["api", "public", "tenants"] and len(segments) >= 4:
        return _normalize_slug(segments[3])

    if lower_segments[:2] == ["api", "public"] and len(segments) >= 3:
        return _normalize_slug(segments[2])

    if lower_segments[:3] == ["api", "pwa", "public"] and len(segments) >= 4:
        return _normalize_slug(segments[3])

    if lower_segments[:2] == ["api", "pwa"] and len(segments) >= 3:
        # Endpoints such as /api/pwa/anon-id and /api/pwa/tenant-info do not
        # include the slug in the path; they pass it via query parameters. Avoid
        # treating those endpoint names as slugs so we can resolve using the
        # query args or widget tokens instead of falling through to a 400.
        if lower_segments[2] in {"anon-id", "tenant-info", "manifest.json", "manifest"}:
            return None

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
        or _tenant_slug_from_body()
        or _tenant_slug_from_url(request.headers.get("Referer") or getattr(request, "referrer", None))
        or request.headers.get("X-Tenant")
    )
    if slug:
        try:
            from services.tenant_resolver import apply_tenant_alias

            slug = apply_tenant_alias(slug)
        except Exception:
            pass
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
        or _tenant_slug_from_url(request.headers.get("Referer") or getattr(request, "referrer", None))
    )
    if slug_hint:
        try:
            from services.tenant_resolver import apply_tenant_alias

            slug_hint = apply_tenant_alias(slug_hint)
        except Exception:
            pass
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

    forwarded_host = request.headers.get("X-Forwarded-Host")
    host_header = forwarded_host or request.host
    host = host_header.split(":", 1)[0].lower() if host_header else None
    if host:
        mapping = current_app.config.get("TENANT_DOMAIN_MAP", {})
        mapped = mapping.get(host)
        if mapped:
            tenant = _find_tenant_by_slug(mapped)
            if tenant:
                return tenant

        # Fall back to the shared resolver so custom domains configured on the
        # tenant (``tenant.dominio``) are honored even when ``TENANT_DOMAIN_MAP``
        # is not set. This mirrors the logic of ``resolve_tenant_only`` used by
        # public endpoints and prevents 400 responses on vanity domains.
        from services.tenant_resolver import TenantResolutionError, resolve_tenant_only

        try:
            tenant = resolve_tenant_only(host=host, require_explicit_slug=False)
        except TenantResolutionError:
            tenant = None

        if tenant:
            return tenant

    # As a defensive fallback, return the first available tenant so
    # anonymous/public endpoints do not hard-fail when at least one tenant
    # exists in the database. This mirrors the lenient behavior of
    # ``resolve_tenant_only(require_explicit_slug=False)`` used by public
    # endpoints and avoids 400/401 responses during domain discovery.

    # SECURITY FIX: Do NOT fallback for API routes. Strict tenant isolation required.
    # Paths starting with /api must have explicit tenant context.
    path = request.path or ""
    if path.startswith("/api"):
        return None

    return _fallback_default_tenant()


def tenant_middleware(app) -> None:
    @app.before_request
    def attach_tenant_profile() -> None:
        tenant = _resolve_tenant_profile()
        g.tenant_profile = tenant
        g.tenant_profile_slug = tenant.slug if tenant else None
        if tenant:
            g.current_tenant = tenant.slug
            g.current_tenant_slug = tenant.slug


def require_tenant(func=None) -> TenantProfile:
    """Use the shared tenant resolver decorator without duplicating logic."""

    if func is not None and callable(func):
        return _decorator_require_tenant(func)

    return _decorator_require_tenant()
