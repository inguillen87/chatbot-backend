"""Alias para exponer el catálogo como ``/productos`` con modo público opcional."""

from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import quote_plus, urlencode

from flask import Blueprint, current_app, g, jsonify, render_template, request
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


def _resolve_public_owner() -> Tuple[Optional[TenantProfile], Optional[User]]:
    """Encuentra el owner asociado al catálogo público solicitado."""

    tenant = getattr(g, "tenant_profile", None)
    owner = getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)
    if owner:
        return tenant, owner

    tenant = _lookup_tenant_by_slug(request.args.get("tenant"))
    if tenant:
        owner = tenant.municipio or tenant.pyme
        if owner:
            return tenant, owner

    tenant_id = _coerce_int(request.args.get("tenant_id"))
    if tenant_id:
        tenant = TenantProfile.query.get(tenant_id)
        if tenant:
            owner = tenant.municipio or tenant.pyme
            if owner:
                return tenant, owner

    owner_id = _coerce_int(
        request.args.get("owner_id")
        or request.args.get("pyme_id")
        or request.args.get("municipio_id")
    )
    if owner_id:
        owner = User.query.get(owner_id)
        if owner:
            return None, owner

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

    return None, owner


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

    tenant, owner = _resolve_public_owner()
    if not owner:
        return jsonify({"error": "Catálogo no disponible"}), 404

    ensure_seed_catalog(owner, tenant)

    if view_mode and view_mode not in {"json", "api"}:
        catalog_response = listar_catalogo.__wrapped__(owner)
        productos = []
        try:
            productos = catalog_response.get_json(silent=True) or []  # type: ignore[attr-defined]
        except Exception:
            productos = []

        query_dict = request.args.to_dict(flat=True)
        share_query = urlencode(query_dict) if query_dict else ""
        share_url = request.base_url + (f"?{share_query}" if share_query else "")
        whatsapp_message = quote_plus(
            f"Mirá el catálogo digital de {getattr(owner, 'nombre', 'nuestro comercio')} en Chatboc: {share_url}"
        )
        whatsapp_link = f"https://api.whatsapp.com/send?text={whatsapp_message}"

        return render_template(
            "catalogo_publico.html",
            productos=productos,
            tenant=tenant,
            owner=owner,
            share_url=share_url,
            whatsapp_link=whatsapp_link,
        )

    return listar_catalogo.__wrapped__(owner)
