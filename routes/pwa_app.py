"""Citizen app endpoints for multi-tenant SaaS experience."""

from __future__ import annotations

from typing import Any
from collections import defaultdict
import uuid

from flask import Blueprint, abort, current_app, g, jsonify, request
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from database import db
from models import TenantFollower, TenantProfile, TenantTicket
from services.tenant_claim_receipts import (
    TENANT_CLAIM_INTAKE_RECEIPT_CONTRACT_VERSION,
    TENANT_CLAIM_RECEIPT_SECRET_VERSION,
    TenantClaimReceiptSecretUnavailable,
    TenantClaimValidationError,
    build_tenant_claim_intake_receipt,
    normalize_idempotency_key,
    normalize_tenant_claim_payload,
    tenant_claim_idempotency_hash,
    tenant_claim_payload_matches,
    tenant_claim_receipt_secret,
)
from utils.auth_decorators import require_auth, require_auth_optional
from utils.fingerprint import hash_fingerprint
from utils.time_utils import datetime_to_iso_utc
from middleware import require_tenant


pwa_app_bp = Blueprint("pwa_app", __name__, url_prefix="/api/pwa/app")
# Blueprint con rutas espejo para compatibilidad con versiones previas de la PWA
pwa_app_legacy_bp = Blueprint("pwa_app_legacy", __name__, url_prefix="/app")


def _claim_intake_request_id() -> str:
    incoming = str(
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or ""
    ).strip()
    if incoming and len(incoming) <= 128 and all(32 <= ord(char) < 127 for char in incoming):
        return incoming
    return uuid.uuid4().hex


def _claim_intake_json(payload: dict[str, Any], status: int):
    request_id = str(payload.get("request_id") or _claim_intake_request_id())
    body = dict(payload)
    body["request_id"] = request_id
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _tenant_ticket_list_json(payload: dict[str, Any]):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _claim_intake_error(
    reason_code: str,
    message: str,
    status: int,
    *,
    request_id: str,
    retryable: bool = False,
):
    return _claim_intake_json(
        {
            "contract_version": TENANT_CLAIM_INTAKE_RECEIPT_CONTRACT_VERSION,
            "ok": False,
            "persisted": False,
            "reason_code": reason_code,
            "retryable": retryable,
            "error": {"code": status, "message": message},
            "request_id": request_id,
        },
        status,
    )


def _find_ticket_by_intake_hash(tenant_id: int, idempotency_hash: str) -> TenantTicket | None:
    return TenantTicket.query.filter_by(
        tenant_id=tenant_id,
        intake_idempotency_hash=idempotency_hash,
    ).first()


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
    request_id = _claim_intake_request_id()
    canonical_route = request.blueprint != pwa_app_legacy_bp.name

    try:
        idempotency_key = normalize_idempotency_key(
            request.headers.get("Idempotency-Key"),
            required=canonical_route,
        )
        secret = tenant_claim_receipt_secret(TENANT_CLAIM_RECEIPT_SECRET_VERSION)
        normalized = normalize_tenant_claim_payload(request.get_json(silent=True))
    except TenantClaimValidationError as exc:
        return _claim_intake_error(
            exc.reason_code,
            exc.public_message,
            400,
            request_id=request_id,
        )
    except TenantClaimReceiptSecretUnavailable:
        return _claim_intake_error(
            "claim_receipt_unavailable",
            "No podemos emitir el comprobante de seguimiento en este momento.",
            503,
            request_id=request_id,
            retryable=True,
        )

    idempotency_hash = None
    if idempotency_key is not None:
        idempotency_hash = tenant_claim_idempotency_hash(
            tenant_id=tenant.id,
            key=idempotency_key,
            secret=secret,
        )
        existing = _find_ticket_by_intake_hash(tenant.id, idempotency_hash)
        if existing is not None:
            if not tenant_claim_payload_matches(existing, normalized.payload_hash):
                return _claim_intake_error(
                    "idempotency_key_conflict",
                    "Idempotency-Key ya fue usado con otro reclamo.",
                    409,
                    request_id=request_id,
                )
            try:
                receipt = build_tenant_claim_intake_receipt(
                    existing,
                    deduplicated=True,
                    request_id=request_id,
                    secret=secret,
                )
            except TenantClaimReceiptSecretUnavailable:
                return _claim_intake_error(
                    "claim_receipt_unavailable",
                    "No podemos emitir el comprobante de seguimiento en este momento.",
                    503,
                    request_id=request_id,
                    retryable=True,
                )
            return _claim_intake_json(receipt, 200)

    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=getattr(getattr(g, "viewer", None), "id", None),
        categoria=normalized.categoria,
        descripcion=normalized.descripcion,
        datos_extra=normalized.extras,
        origen="pwa",
        latitud=normalized.latitud,
        longitud=normalized.longitud,
        fingerprint=hash_fingerprint(request),
        intake_idempotency_hash=idempotency_hash,
        intake_payload_hash=normalized.payload_hash,
        claim_receipt_secret_version=TENANT_CLAIM_RECEIPT_SECRET_VERSION,
    )
    db.session.add(ticket)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if idempotency_hash is None:
            raise
        raced = _find_ticket_by_intake_hash(tenant.id, idempotency_hash)
        if raced is None:
            raise
        if not tenant_claim_payload_matches(raced, normalized.payload_hash):
            return _claim_intake_error(
                "idempotency_key_conflict",
                "Idempotency-Key ya fue usado con otro reclamo.",
                409,
                request_id=request_id,
            )
        try:
            receipt = build_tenant_claim_intake_receipt(
                raced,
                deduplicated=True,
                request_id=request_id,
                secret=secret,
            )
        except TenantClaimReceiptSecretUnavailable:
            return _claim_intake_error(
                "claim_receipt_unavailable",
                "No podemos emitir el comprobante de seguimiento en este momento.",
                503,
                request_id=request_id,
                retryable=True,
            )
        return _claim_intake_json(receipt, 200)

    try:
        receipt = build_tenant_claim_intake_receipt(
            ticket,
            deduplicated=False,
            request_id=request_id,
            secret=secret,
        )
    except TenantClaimReceiptSecretUnavailable:
        return _claim_intake_error(
            "claim_receipt_unavailable",
            "El reclamo fue registrado, pero no podemos emitir el comprobante en este momento.",
            503,
            request_id=request_id,
            retryable=True,
        )
    return _claim_intake_json(receipt, 201)


@pwa_app_bp.get("/tickets")
@require_auth_optional
def list_tickets():
    tenant = require_tenant()
    user = getattr(g, "viewer", None)
    fingerprint = hash_fingerprint(request)

    visibility_filters = []
    if user is not None:
        # An authenticated claimant always keeps access to their own rows,
        # including receipt-backed T- claims.
        viewer_id = getattr(user, "id", None)
        if viewer_id is not None:
            visibility_filters.append(TenantTicket.user_id == viewer_id)

    if fingerprint:
        # Compatibility boundary: fingerprint lookup is retained exclusively
        # for pre-receipt legacy rows. Receipt-backed claims require an actual
        # authenticated ticket owner; this PWA route grants no backoffice role
        # override.
        visibility_filters.append(
            and_(
                TenantTicket.claim_receipt_secret_version.is_(None),
                TenantTicket.intake_idempotency_hash.is_(None),
                TenantTicket.intake_payload_hash.is_(None),
                TenantTicket.fingerprint == fingerprint,
            )
        )

    if not visibility_filters:
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
        return _tenant_ticket_list_json(empty_payload)

    base_filters = [
        TenantTicket.tenant_id == tenant.id,
        or_(*visibility_filters),
    ]

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

    return _tenant_ticket_list_json(payload)


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
