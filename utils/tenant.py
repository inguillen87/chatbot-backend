"""Tenant utilities and decorator helpers for multitenant endpoints."""

from __future__ import annotations

from functools import wraps
from typing import Optional

from flask import current_app, g, request
from sqlalchemy import func

from models import TenantProfile
from utils.errors import ApiError

TENANT_HEADER = "X-Tenant"
TENANT_SLUG_HEADER = "X-Tenant-Slug"
TENANT_QUERY_KEYS = ("tenant", "tenant_slug", "municipio_slug", "slug")
TENANT_HEADER_KEYS = ("X-Chatboc-Tenant", "X-Tenant", "X-CHATBOC-TENANT", "X-TENANT")


def get_current_tenant() -> Optional[str]:
    """
    Devuelve el tenant actual SIN romper si falta user.tenant_id.

    Prioridades:
    1) g.current_tenant / g.tenant_slug (si ya fue seteado antes)
    2) Querystring (?tenant= / ?tenant_slug= / ?municipio_slug= / ?slug=)
    3) Headers (X-Chatboc-Tenant / X-Tenant)
    4) Fallback: si la ruta contiene /municipio/ asumimos 'municipio'
    5) Si nada funciona: recién ahí tiramos ApiError("tenant requerido", 400)
    """

    # 1) Contexto ya resuelto
    for attr in ("current_tenant", "tenant_slug", "tenant"):
        val = getattr(g, attr, None)
        if val:
            return val

    # 2) Querystring (lo que llega desde /market/municipio/cart)
    for key in TENANT_QUERY_KEYS:
        val = request.args.get(key)
        if val:
            g.current_tenant = val
            g.tenant_slug = val
            g.tenant = val
            return val

    # 3) Headers (para widget embebido / integraciones)
    for key in TENANT_HEADER_KEYS:
        val = request.headers.get(key)
        if val:
            g.current_tenant = val
            g.tenant_slug = val
            g.tenant = val
            return val

    # 4) Usuario autenticado (g.current_user / g.user / g.viewer)
    for attr in ("current_user", "user", "viewer"):
        user = getattr(g, attr, None)
        if not user:
            continue

        tenant_slug = getattr(user, "tenant_slug", None)
        profile_slug = getattr(getattr(user, "tenant_profile", None), "slug", None)
        municipio_slug = getattr(getattr(user, "tenant_profile_municipio", None), "slug", None)
        pyme_slug = getattr(getattr(user, "tenant_profile_pyme", None), "slug", None)
        municipio_id = getattr(user, "municipio_id", None)
        pyme_id = getattr(user, "pyme_id", None)
        tipo_chat = getattr(user, "tipo_chat", None)

        slug = tenant_slug or profile_slug or municipio_slug or pyme_slug
        if not slug and municipio_id:
            tenant = TenantProfile.query.filter_by(municipio_id=municipio_id).first()
            slug = getattr(tenant, "slug", None)
            if not slug and tipo_chat == "municipio":
                slug = "municipio"

        if not slug and pyme_id:
            tenant = TenantProfile.query.filter_by(pyme_id=pyme_id).first()
            slug = getattr(tenant, "slug", None)
            if not slug and tipo_chat == "pyme":
                slug = "pyme"

        if not slug and tipo_chat == "municipio":
            slug = "municipio"

        if not slug and tipo_chat == "pyme":
            slug = "pyme"

        if slug:
            g.current_tenant = slug
            g.tenant_slug = slug
            g.tenant = slug
            return slug

    # 5) Fallback ultra defensivo para rutas legacy /municipio/
    if "/municipio/" in request.path:
        g.current_tenant = "municipio"
        g.tenant_slug = "municipio"
        g.tenant = "municipio"
        return "municipio"

    # 5) Si realmente no se puede resolver
    current_app.logger.warning(
        "[tenant-debug] tenant faltante | path=%s | qs=%s | headers=%s | cookies=%s",
        request.path,
        dict(request.args),
        {k: request.headers.get(k) for k in request.headers.keys() if "tenant" in k.lower()},
        dict(request.cookies),
    )
    raise ApiError("tenant requerido", 400)


def get_current_tenant_slug() -> Optional[str]:
    try:
        return get_current_tenant()
    except ApiError:
        return None


def get_current_tenant_profile(slug: Optional[str] = None) -> Optional[TenantProfile]:
    """Return the tenant associated with the current request if any."""

    slug = slug or get_current_tenant_slug()
    if not slug:
        slug = None

    tenant: Optional[TenantProfile] = None

    if slug:
        try:
            from services.tenant_resolver import apply_tenant_alias

            slug = apply_tenant_alias(slug)
        except Exception:
            pass

        tenant = (
            TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower())
            .order_by(TenantProfile.id.asc())
            .first()
        )

    if tenant:
        _store_tenant_in_context(tenant)
        return tenant

    # Fallback: rely on the shared resolver so widget tokens, host hints and
    # relaxed slug requirements behave the same across endpoints.
    from services.tenant_resolver import resolve_tenant_only, TenantResolutionError

    widget_token = (
        request.headers.get("X-Widget-Token")
        or request.headers.get("X-Entity-Token")
        or request.args.get("widget_token")
        or request.args.get("entity_token")
        or request.args.get("entityToken")
    )

    host_hint = request.headers.get("X-Forwarded-Host") or request.host

    try:
        tenant = resolve_tenant_only(
            tenant_slug=slug,
            widget_token=widget_token,
            host=host_hint,
            require_explicit_slug=False,
        )
    except TenantResolutionError:
        tenant = None

    if not tenant:
        fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        if fallback_slug:
            tenant = (
                TenantProfile.query.filter(
                    func.lower(TenantProfile.slug) == fallback_slug.lower()
                )
                .order_by(TenantProfile.id.asc())
                .first()
            )

    if not tenant:
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

    if tenant:
        _store_tenant_in_context(tenant)

    return tenant


def _store_tenant_in_context(tenant: TenantProfile) -> None:
    g.tenant_profile = tenant
    g.tenant_profile_slug = getattr(tenant, "slug", None)
    g.current_tenant = getattr(tenant, "slug", None) or tenant
    g.current_tenant_slug = getattr(tenant, "slug", None)


def require_tenant(func=None):
    """Decorator (and direct helper) ensuring a tenant exists in the request context."""

    def _ensure_tenant():
        tenant = getattr(g, "tenant_profile", None)
        if tenant:
            return tenant

        tenant_slug = get_current_tenant()
        tenant = get_current_tenant_profile(tenant_slug)
        if tenant:
            _store_tenant_in_context(tenant)
            return tenant

        raise ApiError("tenant requerido", 400)

    if func is None:
        tenant = _ensure_tenant()
        if tenant:
            return tenant

    @wraps(func)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return func(*args, **kwargs)

        _ensure_tenant()

        return func(*args, **kwargs)

    return wrapper

