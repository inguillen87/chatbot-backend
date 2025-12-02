"""Citizen app endpoints for multi-tenant SaaS experience."""

from __future__ import annotations

from typing import Any
from collections import defaultdict

from flask import Blueprint, abort, current_app, g, jsonify, request
from sqlalchemy import func, or_

from database import db
from models import TenantFollower, TenantProfile, TenantTicket
from utils.auth_decorators import require_auth, require_auth_optional
from utils.fingerprint import hash_fingerprint
from utils.time_utils import datetime_to_iso_utc
from middleware import require_tenant


pwa_app_bp = Blueprint("pwa_app", __name__, url_prefix="/api/pwa/app")
# Blueprint con rutas espejo para compatibilidad con versiones previas de la PWA
pwa_app_legacy_bp = Blueprint("pwa_app_legacy", __name__, url_prefix="/app")


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_estado(value: str | None) -> str:
    if not value:
        return "desconocido"
    return "resuelto" if value == "cerrado" else value


def _serialize_tenant_ticket(ticket: TenantTicket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "tenant_id": ticket.tenant_id,
        "estado": _normalize_estado(ticket.estado),
        "categoria": ticket.categoria,
        "descripcion": ticket.descripcion,
        "latitud": ticket.latitud,
        "longitud": ticket.longitud,
        "datos_extra": ticket.datos_extra or {},
        "origen": ticket.origen,
        "created_at": datetime_to_iso_utc(ticket.created_at),
        "updated_at": datetime_to_iso_utc(ticket.updated_at),
    }


@pwa_app_bp.get("/me/tenants")
@require_auth_optional
def list_followed_tenants():
    user = getattr(g, "viewer", None)
    if user is None:
        return jsonify([])
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
    tenant = require_tenant()
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


@pwa_app_bp.get("/tickets")
@require_auth_optional
def list_tickets():
    tenant = require_tenant()
    user = getattr(g, "viewer", None)
    fingerprint = hash_fingerprint(request)

    base_filters = [TenantTicket.tenant_id == tenant.id]
    if user and fingerprint:
        base_filters.append(or_(TenantTicket.user_id == user.id, TenantTicket.fingerprint == fingerprint))
    elif user:
        base_filters.append(TenantTicket.user_id == user.id)
    elif fingerprint:
        base_filters.append(TenantTicket.fingerprint == fingerprint)
    else:
        empty_payload = {
            "tickets": [],
            "summary": {"nuevo": 0, "en_proceso": 0, "resuelto": 0, "otros": 0, "total": 0},
            "pagination": {
                "page": 1,
                "per_page": 0,
                "total_items": 0,
                "total_pages": 1,
                "has_next": False,
                "has_prev": False,
            },
        }
        return jsonify(empty_payload)

    requested_estado = request.args.get("estado")

    base_query = TenantTicket.query.filter(*base_filters)

    summary_counts = defaultdict(int)
    status_rows = (
        base_query.with_entities(TenantTicket.estado, func.count(TenantTicket.id))
        .group_by(TenantTicket.estado)
        .all()
    )

    total_items = 0
    for estado_original, cantidad in status_rows:
        estado_normalizado = _normalize_estado(estado_original)
        if estado_normalizado not in ("nuevo", "en_proceso", "resuelto"):
            summary_counts["otros"] += cantidad
        else:
            summary_counts[estado_normalizado] += cantidad
        total_items += cantidad

    summary_counts.setdefault("nuevo", 0)
    summary_counts.setdefault("en_proceso", 0)
    summary_counts.setdefault("resuelto", 0)
    summary_counts.setdefault("otros", 0)
    summary_counts["total"] = total_items

    filtered_query = base_query
    if requested_estado and requested_estado != "todos":
        if requested_estado == "resuelto":
            filtered_query = filtered_query.filter(TenantTicket.estado.in_(["resuelto", "cerrado"]))
        else:
            filtered_query = filtered_query.filter(TenantTicket.estado == requested_estado)

    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    default_per_page = current_app.config.get("PWA_TICKETS_PER_PAGE_DEFAULT", 20)
    per_page_raw = request.args.get("per_page")
    try:
        per_page = int(per_page_raw) if per_page_raw is not None else int(default_per_page)
    except (TypeError, ValueError):
        per_page = int(default_per_page)

    if per_page <= 0:
        per_page = 0
        page = 1

    ordered_query = filtered_query.order_by(TenantTicket.created_at.desc())
    if per_page > 0:
        tickets = (
            ordered_query.offset((page - 1) * per_page).limit(per_page).all()
        )
    else:
        tickets = ordered_query.all()

    pagination = {
        "page": page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": max(1, (total_items + per_page - 1) // per_page) if per_page else 1,
        "has_next": per_page > 0 and page * per_page < total_items,
        "has_prev": per_page > 0 and page > 1,
    }

    payload = {
        "tickets": [_serialize_tenant_ticket(ticket) for ticket in tickets],
        "summary": dict(summary_counts),
        "pagination": pagination,
    }

    return jsonify(payload)


# --- Rutas espejo para compatibilidad con clientes antiguos ---
pwa_app_legacy_bp.add_url_rule(
    "/me/tenants",
    view_func=list_followed_tenants,
    methods=["GET"],
)
pwa_app_legacy_bp.add_url_rule(
    "/me/tenants/follow",
    view_func=follow_tenant,
    methods=["POST"],
)
pwa_app_legacy_bp.add_url_rule(
    "/me/tenants/follow",
    view_func=unfollow_tenant,
    methods=["DELETE"],
)
pwa_app_legacy_bp.add_url_rule(
    "/tickets",
    view_func=create_ticket,
    methods=["POST"],
)
pwa_app_legacy_bp.add_url_rule(
    "/tickets",
    view_func=list_tickets,
    methods=["GET"],
)
