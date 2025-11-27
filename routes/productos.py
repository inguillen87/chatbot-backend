"""Alias para exponer el catálogo como ``/productos`` con modo público opcional."""

from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import urlencode

from flask import Blueprint, current_app, g, jsonify, redirect, request
from flask_cors import cross_origin
from flask_login import current_user
from sqlalchemy import func

from models import TenantProfile, User
from routes.catalogo import listar_catalogo
from services.catalog_seed import ensure_seed_catalog
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
    normalized = slug.strip().lower()
    if not normalized:
        return None
    return TenantProfile.query.filter(func.lower(TenantProfile.slug) == normalized).first()


def _tenant_slug_from_path(path: str | None) -> Optional[str]:
    if not path:
        return None

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None

    prefix = segments[0].lower()
    slug = segments[1]

    if prefix in {"municipio", "municipios", "m", "pyme", "pymes", "p"}:
        cleaned = slug.strip().lower()
        return cleaned or None

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

    path_tenant_slug = _tenant_slug_from_path(request.path)

    tenant_slug = (
        request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or path_tenant_slug
    )
    tenant_id = _coerce_int(request.headers.get("X-Tenant-Id") or request.args.get("tenant_id"))
    has_explicit_hint = bool(
        tenant_slug
        or tenant_id
        or request.headers.get("X-Widget-Token")
        or request.args.get("widget_token")
        or path_tenant_slug
    )

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
            tenant = getattr(owner, "tenant_profile", None)
            return tenant, owner

    if require_explicit and not has_explicit_hint:
        return None, None

    default_tenant_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if default_tenant_slug:
        tenant = _lookup_tenant_by_slug(default_tenant_slug)
        if tenant:
            owner = tenant.municipio or tenant.pyme
            if owner:
                return tenant, owner

    preferred_tipo = request.args.get("tipo") or "municipio"
    owner = (
        User.query.filter(func.lower(User.tipo_chat) == preferred_tipo.lower())
        .order_by(User.id.asc())
        .first()
    ) or User.query.order_by(User.id.asc()).first()

    tenant = getattr(owner, "tenant_profile", None)
    return tenant, owner


@productos_bp.route("", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs())
def obtener_productos():
    """Devuelve el catálogo de productos, autenticado o público."""

    if request.method == "OPTIONS":
        return "", 204

    view_mode = (request.args.get("view") or "").strip().lower()

    user = _resolve_authenticated_user()
    if user and not view_mode:
        return listar_catalogo.__wrapped__(user)

    tenant, owner = _resolve_public_owner(require_explicit=True)
    if not owner or not tenant:
        return jsonify({"error": "Tenant requerido para catálogo"}), 400

    ensure_seed_catalog(owner, tenant)

    if view_mode and view_mode not in {"json", "api"}:
        query_dict = request.args.to_dict(flat=True)
        share_query = urlencode(query_dict) if query_dict else ""

        frontend_base = (
            current_app.config.get("WIDGET_URL")
            or current_app.config.get("PANEL_URL")
            or request.host_url.rstrip("/")
        )
        tenant_slug = None
        if tenant and getattr(tenant, "slug", None):
            tenant_slug = str(tenant.slug).strip().strip("/")
        path_prefix = f"/{tenant_slug}" if tenant_slug else ""
        target_base = f"{frontend_base.rstrip('/')}{path_prefix}/productos"
        redirect_url = target_base + (f"?{share_query}" if share_query else "")

        return redirect(redirect_url, code=302)

    return listar_catalogo.__wrapped__(owner)
