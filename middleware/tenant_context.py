"""Resolve tenant context for public and citizen endpoints."""

from __future__ import annotations

from typing import Optional

from flask import current_app, g, request
from sqlalchemy import func

from models import TenantProfile


def _normalize_slug(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    slug = value.strip().lower()
    return slug or None


def _find_tenant_by_slug(slug: str) -> Optional[TenantProfile]:
    if not slug:
        return None
    return TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower()).first()


def _tenant_slug_from_path(path: str | None) -> Optional[str]:
    """Return a tenant slug hinted via URL path segments.

    Supported patterns (case-insensitive):
    * /municipio/<slug>/...
    * /municipios/<slug>/...
    * /m/<slug>/...
    * /pyme/<slug>/...
    * /pymes/<slug>/...
    * /p/<slug>/...
    """

    if not path:
        return None

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None

    prefix = segments[0].lower()
    slug = segments[1]

    if prefix in {"municipio", "municipios", "m", "pyme", "pymes", "p"}:
        return _normalize_slug(slug)

    return None


def _resolve_tenant_profile() -> Optional[TenantProfile]:
    slug = _normalize_slug(request.headers.get("X-Tenant"))
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

    slug = _normalize_slug(request.args.get("tenant"))
    if slug:
        tenant = _find_tenant_by_slug(slug)
        if tenant:
            return tenant

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
