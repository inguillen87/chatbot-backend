"""Alias para exponer el catálogo como ``/productos`` con modo público opcional."""

from __future__ import annotations

from typing import Optional, Tuple

from flask import Blueprint, current_app, g, jsonify, request
from flask_login import current_user
from sqlalchemy import func

from models import TenantProfile, User
from routes.catalogo import listar_catalogo
from services.catalog_seed import ensure_seed_catalog
from utils.auth_helpers import obtener_token, user_from_token

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


@productos_bp.route("", methods=["GET"], strict_slashes=False)
def obtener_productos():
    """Devuelve el catálogo de productos, autenticado o público."""

    user = _resolve_authenticated_user()
    if user:
        return listar_catalogo.__wrapped__(user)

    tenant, owner = _resolve_public_owner()
    if not owner:
        return jsonify({"error": "Catálogo no disponible"}), 404

    ensure_seed_catalog(owner, tenant)
    return listar_catalogo.__wrapped__(owner)
