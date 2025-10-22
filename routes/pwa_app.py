"""Citizen app endpoints for multi-tenant SaaS experience."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, g, jsonify, request
from sqlalchemy import func

from database import db
from models import TenantFollower, TenantProfile, TenantTicket
from utils.auth_decorators import require_auth, require_auth_optional
from utils.fingerprint import hash_fingerprint


pwa_app_bp = Blueprint("pwa_app", __name__, url_prefix="/api/pwa/app")


def _require_tenant() -> TenantProfile:
    tenant = getattr(g, "tenant_profile", None)
    if tenant is None:
        abort(400, description="Debe indicar un tenant")
    return tenant


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@pwa_app_bp.get("/me/tenants")
@require_auth
def list_followed_tenants():
    user = g.viewer
    rows = (
        TenantFollower.query.join(TenantProfile)
        .filter(TenantFollower.user_id == user.id)
        .order_by(TenantProfile.nombre.asc())
        .all()
    )
    return jsonify(
        [
            {
                "tenant_id": follower.tenant_id,
                "slug": follower.tenant.slug,
                "nombre": follower.tenant.nombre,
                "notifications_enabled": follower.notifications_enabled,
            }
            for follower in rows
        ]
    )


@pwa_app_bp.post("/me/tenants/follow")
@require_auth
def follow_tenant():
    user = g.viewer
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or payload.get("tenant") or "").strip().lower()
    if not slug:
        abort(400, description="Debe indicar el tenant a seguir")

    tenant = (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug)
        .first()
    )
    if tenant is None:
        abort(404, description="Tenant no encontrado")

    follower = TenantFollower.query.filter_by(user_id=user.id, tenant_id=tenant.id).first()
    notifications = payload.get("notifications_enabled")
    if follower:
        if notifications is not None:
            follower.notifications_enabled = bool(notifications)
    else:
        follower = TenantFollower(
            user_id=user.id,
            tenant_id=tenant.id,
            notifications_enabled=True if notifications is None else bool(notifications),
        )
        db.session.add(follower)

    db.session.commit()
    return jsonify(
        {
            "tenant_id": tenant.id,
            "slug": tenant.slug,
            "notifications_enabled": follower.notifications_enabled,
        }
    )


@pwa_app_bp.delete("/me/tenants/follow")
@require_auth
def unfollow_tenant():
    user = g.viewer
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or payload.get("tenant") or "").strip().lower()
    if not slug:
        abort(400, description="Debe indicar el tenant a dejar de seguir")

    tenant = (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug)
        .first()
    )
    if tenant is None:
        return jsonify({"removed": False}), 200

    follower = TenantFollower.query.filter_by(user_id=user.id, tenant_id=tenant.id).first()
    if not follower:
        return jsonify({"removed": False}), 200

    db.session.delete(follower)
    db.session.commit()
    return jsonify({"removed": True})


@pwa_app_bp.post("/tickets")
@require_auth_optional
def create_ticket():
    tenant = _require_tenant()
    payload = request.get_json(silent=True) or {}
    descripcion_raw = payload.get("descripcion")
    descripcion = descripcion_raw.strip() if isinstance(descripcion_raw, str) else None
    if not descripcion:
        abort(400, description="Debe indicar la descripción del reclamo")

    categoria_raw = payload.get("categoria")
    categoria = categoria_raw.strip() if isinstance(categoria_raw, str) else None

    extras = payload.get("extras")
    if not isinstance(extras, dict):
        extras = None

    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=getattr(getattr(g, "viewer", None), "id", None),
        categoria=categoria,
        descripcion=descripcion,
        datos_extra=extras,
        origen="pwa",
        latitud=_coerce_float(payload.get("lat")),
        longitud=_coerce_float(payload.get("lng")),
        fingerprint=hash_fingerprint(request),
    )
    db.session.add(ticket)
    db.session.commit()

    return jsonify({"ticket_id": ticket.id, "estado": ticket.estado}), 201
