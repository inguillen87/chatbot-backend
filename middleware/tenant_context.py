"""Resolve tenant context for public and citizen endpoints."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from flask import current_app, g, jsonify, make_response, request
from werkzeug.exceptions import HTTPException
from sqlalchemy import func

from database import db
from models import TenantProfile
from utils.tenant import (
    get_current_tenant_profile,
    get_current_tenant_slug,
    require_tenant as _decorator_require_tenant,
)

_TENANT_HINT_BODY_MAX_BYTES = 65_536


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

    # Tenant hints are tiny. Never materialize unknown-length or large request
    # bodies inside global middleware; the owning endpoint must apply its own
    # bounded parser and can resolve tenant identity from authenticated context,
    # headers, or query parameters.
    content_length = request.content_length
    if (
        content_length is None
        or content_length < 0
        or content_length > _TENANT_HINT_BODY_MAX_BYTES
    ):
        return None

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
    return TenantProfile.query.filter(
        func.lower(TenantProfile.slug) == slug.lower(),
        TenantProfile.is_active.is_(True),
    ).first()


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

    return (
        TenantProfile.query.filter(TenantProfile.is_active.is_(True))
        .order_by(TenantProfile.id.asc())
        .first()
    )


def _find_tenant_by_widget_token(token: str) -> Optional[TenantProfile]:
    if not token:
        return None

    normalized = str(token).strip()
    if not normalized:
        return None

    def _has_exact_token(tenant: TenantProfile) -> bool:
        cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
        configured = cfg.get("widget_tokens")
        if isinstance(configured, str):
            values = [configured]
        elif isinstance(configured, (list, tuple, set)):
            values = configured
        else:
            values = []
        return any(str(value or "").strip() == normalized for value in values)

    try:
        candidates = TenantProfile.query.filter(
            TenantProfile.is_active.is_(True),
            TenantProfile.configuracion["widget_tokens"].astext.contains(normalized),
        ).all()
    except Exception:
        candidates = []

    matches = [tenant for tenant in candidates if _has_exact_token(tenant)]
    if not matches:
        matches = [
            tenant
            for tenant in TenantProfile.query.filter(TenantProfile.is_active.is_(True)).all()
            if _has_exact_token(tenant)
        ]
    return matches[0] if len(matches) == 1 else None


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

    # Admin endpoints with explicit tenant path segment, e.g.
    # /api/admin/tenants/<slug>/employees. Allow extracting the slug so
    # the tenant context can be resolved even if no query/header hint was
    # provided by the caller.
    if lower_segments[:3] == ["api", "admin", "tenants"] and len(segments) >= 4:
        return _normalize_slug(segments[3])

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

    # Direct API prefix where the slug comes right after `/api/<slug>/...`.
    if lower_segments[:1] == ["api"] and len(segments) >= 2:
        # Skip prefixes owned by top-level blueprints rather than tenants.
        if lower_segments[1] in {
            "admin",
            "public",
            "pwa",
            "municipal",
            "municipio",
            "auth",
            "v2",
        }:
            return None

        return _normalize_slug(segments[1])

    return None


def _resolve_tenant_profile() -> Optional[TenantProfile]:
    view_args = getattr(request, "view_args", None) or {}

    raw_slug = (
        view_args.get("tenant_slug")
        or view_args.get("tenant")
        or view_args.get("slug")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or _tenant_slug_from_body()
        or request.headers.get("X-Tenant")
        or request.headers.get("X-Tenant-Slug")
        or _tenant_slug_from_path(request.path)
        or _tenant_slug_from_url(request.headers.get("Referer") or getattr(request, "referrer", None))
    )
    slug = _normalize_slug(raw_slug)
    unresolved_explicit_slug = False
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
        unresolved_explicit_slug = True

    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    if tenant_id and not unresolved_explicit_slug:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            tenant = None
        if tenant and getattr(tenant, "is_active", True) is not False:
            return tenant
        # A caller that supplied an explicit database identity must never be
        # silently redirected to a token, host, viewer, or default tenant.
        return None

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
        # Invalid and ambiguous tokens are authoritative failed selectors.
        # Falling through here would expose whichever tenant is configured as
        # the shared-platform default.
        return None

    # An explicit but unknown/inactive slug must never fall through to a host,
    # authenticated viewer, configured default, or first-row tenant. A valid
    # widget token above remains an intentional compatibility selector.
    if unresolved_explicit_slug:
        return None

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
            return None

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

    # Helper: try to reuse an authenticated user (viewer or Flask-Login current_user)
    def _tenant_from_viewer():
        viewer = getattr(g, "viewer", None)
        if not viewer:
            from flask_login import current_user

            if getattr(current_user, "is_authenticated", False):
                viewer = current_user

        if not viewer:
            return None

        # 1. Try explicit tenant_id
        t_id = getattr(viewer, "tenant_id", None)
        if t_id:
            try:
                tenant = TenantProfile.query.get(int(t_id))
                if tenant:
                    return tenant
            except Exception:
                pass

        # 2. Try tenant_slug
        t_slug = getattr(viewer, "tenant_slug", None)
        if t_slug:
            tenant = _find_tenant_by_slug(t_slug)
            if tenant:
                return tenant

        return None

    if path.startswith("/api"):
        tenant = _tenant_from_viewer()
        if tenant:
            return tenant

        return None

    tenant = _tenant_from_viewer()
    if tenant:
        return tenant

    return _fallback_default_tenant()


def tenant_middleware(app) -> None:
    @app.before_request
    def attach_tenant_profile() -> None:
        # Reset every tenant alias at the beginning of each request. This also
        # protects test/worker code that intentionally reuses an app context;
        # a failed resolution must never inherit the previous request's tenant.
        for key in (
            "tenant_profile",
            "tenant_profile_slug",
            "current_tenant",
            "current_tenant_slug",
            "tenant_slug",
            "tenant",
        ):
            g.pop(key, None)

        try:
            tenant = _resolve_tenant_profile()
        except Exception as exc:
            current_app.logger.warning("[tenant_context] failed to resolve tenant context: %s", exc)
            tenant = None

        g.tenant_profile = tenant
        g.tenant_profile_slug = tenant.slug if tenant else None
        if tenant:
            g.current_tenant = tenant.slug
            g.current_tenant_slug = tenant.slug


def require_tenant(func=None) -> TenantProfile:
    """Use the shared tenant resolver decorator without duplicating logic."""

    if func is not None and callable(func):
        return _decorator_require_tenant(func)

    tenant = _resolve_tenant_profile()
    if tenant:
        g.tenant_profile = tenant
        g.tenant_profile_slug = tenant.slug
        g.current_tenant = tenant.slug
        g.current_tenant_slug = tenant.slug
        return tenant

    return _decorator_require_tenant()
