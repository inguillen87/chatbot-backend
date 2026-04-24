"""Alias para exponer el catálogo como ``/productos`` con modo público opcional."""

from __future__ import annotations

import unicodedata
from typing import Optional, Tuple
from urllib.parse import urlencode, urlparse

from flask import Blueprint, current_app, g, jsonify, redirect, request
from flask_cors import cross_origin
from flask_login import current_user
from sqlalchemy import func

from models import TenantProfile, User
from middleware import require_tenant
from routes.catalogo import listar_catalogo
from services.catalog_seed import ensure_seed_catalog
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
from utils.auth_helpers import obtener_token, user_from_token

from config import ALLOWED_ORIGINS

_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]

_CORS_EXPOSE_HEADERS = ["Content-Type", "Authorization", "X-Anon-Id", "Anon-Id"]


def _cors_kwargs() -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _CORS_ALLOWED_HEADERS,
        "expose_headers": _CORS_EXPOSE_HEADERS,
        "methods": ["GET", "OPTIONS"],
    }


productos_bp = Blueprint("productos", __name__, url_prefix="/productos")


def _resolve_authenticated_user() -> Optional[User]:
    """Devuelve el usuario autenticado si existe (token o sesión)."""

    if getattr(current_user, "is_authenticated", False):
        return current_user  # type: ignore[return-value]

    token = obtener_token()
    if not token:
        return None

    return user_from_token(token)


def _coerce_int(value: object) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lookup_tenant_by_slug(slug: Optional[str]) -> Optional[TenantProfile]:
    if not slug:
        return None
    normalized = _normalize_slug(slug)
    if not normalized:
        return None

    # Fast path: direct case-insensitive match
    tenant = (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == normalized)
        .order_by(TenantProfile.id.asc())
        .first()
    )
    if tenant:
        return tenant

    # Fallback for slugs with accents/spacing variants stored in DB.
    for candidate in TenantProfile.query.with_entities(TenantProfile).all():
        candidate_slug = getattr(candidate, "slug", None)
        if candidate_slug and _normalize_slug(candidate_slug) == normalized:
            return candidate

    return None


def _normalize_slug(value: str) -> str:
    """Normalize slugs removing accents and harmonizing separators."""

    cleaned = unicodedata.normalize("NFKD", value or "")
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.replace("_", "-")
    cleaned = cleaned.strip().lower()
    cleaned = cleaned.replace(" ", "-")

    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")

    return cleaned.strip("-")


def _tenant_for_user(user: Optional[User]) -> Optional[TenantProfile]:
    if not user:
        return None

    tenant = (
        getattr(user, "tenant_profile", None)
        or getattr(user, "tenant_profile_municipio", None)
        or getattr(user, "tenant_profile_pyme", None)
    )

    if tenant:
        return tenant

    # Algunos usuarios legacy sólo guardan ``municipio_id``/``pyme_id`` sin
    # asociar explícitamente un ``tenant_profile``. Para permitir que el
    # marketplace/autenticación resuelvan correctamente el catálogo y el
    # carrito, intentamos buscar el tenant asociado usando esos campos.
    municipio_id = getattr(user, "municipio_id", None)
    pyme_id = getattr(user, "pyme_id", None)
    if municipio_id:
        tenant = TenantProfile.query.filter_by(municipio_id=municipio_id).first()
        if tenant:
            g.tenant_profile = tenant
            g.tenant_profile_slug = getattr(tenant, "slug", None)
            return tenant
    if pyme_id:
        tenant = TenantProfile.query.filter_by(pyme_id=pyme_id).first()
        if tenant:
            g.tenant_profile = tenant
            g.tenant_profile_slug = getattr(tenant, "slug", None)
            return tenant

    slug = getattr(user, "tenant_slug", None)
    if slug:
        tenant = _lookup_tenant_by_slug(slug)
        if tenant:
            g.tenant_profile = tenant
            g.tenant_profile_slug = getattr(tenant, "slug", None)
            return tenant

    return None


def _first_tenant_with_owner() -> tuple[Optional[TenantProfile], Optional[User]]:
    """Return the first tenant that has an owner configured."""

    tenant = (
        TenantProfile.query.filter(
            (TenantProfile.municipio_id.isnot(None)) | (TenantProfile.pyme_id.isnot(None))
        )
        .order_by(TenantProfile.id.asc())
        .first()
    )
    owner = tenant.municipio or tenant.pyme if tenant else None
    if tenant and owner:
        return tenant, owner
    return None, None


def _tenant_slug_from_path(path: str | None) -> Optional[str]:
    """Best-effort slug extraction from the URL path.

    Supports both legacy prefixes (e.g. ``/municipio/<slug>/...``) and the
    newer direct tenant paths used by the marketplace frontend (``/<slug>/...``).
    """

    if not path:
        return None

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None

    # API aliases like /api/<slug>/productos coming from the widget/PWA.
    if segments[0].lower() == "api" and len(segments) >= 2:
        # Avoid conflicting with prefixes that already encode the slug later
        if segments[1].lower() not in {"public", "pwa"}:
            cleaned = segments[1].strip().lower()
            return cleaned or None

    # Legacy prefixed pattern: /municipio/<slug>/..., /pyme/<slug>/...
    if len(segments) >= 2:
        prefix = segments[0].lower()
        slug = segments[1]
        if prefix in {"municipio", "municipios", "m", "pyme", "pymes", "p", "t", "market", "marketplace", "shop"}:
            cleaned = slug.strip().lower()
            return cleaned or None

    # Marketplace path style: /<slug>/productos, /<slug>/marketplace, etc.
    # Ignore obviously non-tenant prefixes that map to our API paths.
    first_segment = segments[0].strip().lower()
    if first_segment and first_segment not in {"api", "productos", "carrito", "market", "marketplace", "shop"}:
        return first_segment

    return None


def _tenant_slug_from_url(url: str | None) -> Optional[str]:
    """Extrae el slug del tenant desde una URL completa (referer)."""

    if not url:
        return None

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    # Primero intentamos con la ruta (p.ej. https://host/<slug>/productos).
    slug_from_path = _tenant_slug_from_path(parsed.path)
    if slug_from_path:
        return slug_from_path

    # Si no hay slug en la ruta, intentamos detectar un subdominio como slug
    # (e.g. <slug>.chatboc.ar). Esto permite que el marketplace funcione
    # cuando se navega por dominios dedicados.
    hostname = (parsed.hostname or "").split(".")
    if len(hostname) >= 3:  # ej: slug.dominio.com
        candidate = hostname[0].strip().lower()
        return candidate or None

    return None


def _resolve_public_owner(require_explicit: bool = False) -> Tuple[Optional[TenantProfile], Optional[User]]:
    """Encuentra el owner asociado al catálogo público solicitado.

    Si ``require_explicit`` es True, sólo se acepta un tenant indicado por
    slug/ID/headers/token. No se usan valores por defecto para evitar mezclar
    catálogos entre tenants.
    """

    tenant = getattr(g, "tenant_profile", None)
    owner = getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)
    if owner:
        return tenant, owner

    # If the requester is authenticated, honor their tenant even without
    # explicit hints to avoid leaking catalogs across tenants.
    user = _resolve_authenticated_user()
    tenant_for_user = _tenant_for_user(user) if user else None
    if tenant_for_user:
        g.tenant_profile = tenant_for_user
        g.tenant_profile_slug = getattr(tenant_for_user, "slug", None)
        owner = tenant_for_user.municipio or tenant_for_user.pyme or user
        return tenant_for_user, owner

    path_tenant_slug = _tenant_slug_from_path(request.path)
    referrer_slug = _tenant_slug_from_url(request.referrer)

    tenant_slug = (
        request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or path_tenant_slug
        or referrer_slug
    )
    tenant_id = _coerce_int(request.headers.get("X-Tenant-Id") or request.args.get("tenant_id"))
    has_explicit_hint = bool(
        tenant_slug
        or tenant_id
        or request.headers.get("X-Widget-Token")
        or request.args.get("widget_token")
        or path_tenant_slug
        or referrer_slug
    )

    widget_token = (
        request.headers.get("X-Widget-Token")
        or request.headers.get("X-Entity-Token")
        or request.args.get("widget_token")
        or request.args.get("entity_token")
        or request.args.get("entityToken")
    )

    # Try explicit tenant resolution via shared resolver (handles widget tokens/domains)
    allow_resolution = bool(tenant_slug or widget_token or path_tenant_slug or referrer_slug or not require_explicit)
    if allow_resolution:
        try:
            tenant = resolve_tenant_only(
                tenant_slug=tenant_slug or path_tenant_slug or referrer_slug,
                widget_token=widget_token,
                require_explicit_slug=bool(require_explicit and (tenant_slug or path_tenant_slug or referrer_slug or widget_token)),
            )
            if tenant:
                owner = tenant.municipio or tenant.pyme
                if owner:
                    g.tenant_profile = tenant
                    g.tenant_profile_slug = tenant.slug
                    return tenant, owner
                fallback_tenant, fallback_owner = _first_tenant_with_owner()
                if fallback_tenant and fallback_owner:
                    g.tenant_profile = fallback_tenant
                    g.tenant_profile_slug = getattr(fallback_tenant, "slug", None)
                    return fallback_tenant, fallback_owner
        except TenantResolutionError:
            pass

    if tenant_slug:
        tenant = _lookup_tenant_by_slug(tenant_slug)
        if tenant:
            owner = tenant.municipio or tenant.pyme
            if owner:
                g.tenant_profile = tenant
                g.tenant_profile_slug = tenant.slug
                return tenant, owner

    if tenant_id:
        tenant = TenantProfile.query.get(tenant_id)
        if tenant:
            owner = tenant.municipio or tenant.pyme
            if owner:
                g.tenant_profile = tenant
                g.tenant_profile_slug = tenant.slug
                return tenant, owner

    owner_id = _coerce_int(
        request.args.get("owner_id")
        or request.args.get("pyme_id")
        or request.args.get("municipio_id")
    )
    if owner_id:
        owner = User.query.get(owner_id)
        if owner:
            tenant = _tenant_for_user(owner)
            return tenant, owner

    if require_explicit and not has_explicit_hint:
        return None, None

    default_tenant_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if default_tenant_slug:
        tenant = _lookup_tenant_by_slug(default_tenant_slug)
        if tenant:
            owner = tenant.municipio or tenant.pyme
            if owner:
                g.tenant_profile = tenant
                g.tenant_profile_slug = getattr(tenant, "slug", None)
                return tenant, owner

    # Como último recurso (cuando no hay hints ni tenant por defecto),
    # elegimos el primer tenant que tenga un owner asociado para evitar
    # errores 400 en catálogos públicos/marketplace. Esto replica el
    # degradado usado por ``services.tenant_resolver`` y permite navegar el
    # catálogo aunque la app cliente no envíe los headers/parámetros de
    # tenant.
    fallback_tenant, fallback_owner = _first_tenant_with_owner()
    if fallback_tenant and fallback_owner:
        return fallback_tenant, fallback_owner

    return None, None


@productos_bp.route("", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs())
@require_tenant
def obtener_productos():
    """Devuelve el catálogo de productos, autenticado o público."""

    if request.method == "OPTIONS":
        return "", 204

    view_mode = (request.args.get("view") or "").strip().lower()
    accept_header = request.headers.get("Accept", "").lower()

    # Check if this is an API call explicitly asking for JSON
    is_api_call = (
        "application/json" in accept_header or
        view_mode in {"json", "api"}
    )

    user = _resolve_authenticated_user()
    tenant_for_user = _tenant_for_user(user) if user else None
    tenant = getattr(g, "tenant_profile", None) or getattr(g, "current_tenant", None)
    owner_for_user = None

    if user and tenant_for_user and tenant and tenant_for_user.id == tenant.id:
        g.tenant_profile = tenant_for_user
        g.tenant_profile_slug = tenant_for_user.slug
        owner_for_user = tenant_for_user.municipio or tenant_for_user.pyme or user

    owner = owner_for_user or getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)

    if not owner and user:
        owner = user

    if not owner or not tenant:
        return jsonify({"error": "Tenant requerido para catálogo"}), 400

    ensure_seed_catalog(owner, tenant)

    # 1. BROWSER REDIRECT: If it's a browser request (not API) and contains view params,
    # we redirect to the *external* frontend URL to avoid loops.
    if view_mode and not is_api_call:
        query_dict = request.args.to_dict(flat=True)
        share_query = urlencode(query_dict) if query_dict else ""

        frontend_base = (
            current_app.config.get("WIDGET_URL")
            or current_app.config.get("PANEL_URL")
            or request.host_url.rstrip("/")
        )

        # If we are in local dev and no external frontend is defined, we can't redirect safely
        # to prevent a loop if the backend is serving the same URL.
        # Check if frontend_base is the same as current host.
        current_host = request.host_url.rstrip("/")
        is_same_host = frontend_base.rstrip("/") == current_host

        # If external frontend is configured (different host), redirect there.
        if not is_same_host:
            tenant_slug = None
            if tenant and getattr(tenant, "slug", None):
                tenant_slug = str(tenant.slug).strip().strip("/")
            path_prefix = f"/{tenant_slug}" if tenant_slug else ""
            target_base = f"{frontend_base.rstrip('/')}{path_prefix}/productos"
            redirect_url = target_base + (f"?{share_query}" if share_query else "")
            return redirect(redirect_url, code=302)

        # If we are on the same host (backend=frontend or missing config),
        # we can't redirect to ourselves. We must serve JSON or a message.
        # Since we deleted the HTML template per user request, we fall back to JSON
        # but maybe wrap it or just serve it directly.
        # For now, let's serve the JSON data so at least the data is visible.
        pass

    # 2. API RESPONSE: Return JSON data
    return listar_catalogo.__wrapped__(owner)
