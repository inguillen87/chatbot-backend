from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any
import uuid

from flask import Blueprint, jsonify, request
import requests

from extensions import db
from models import MarketOrder, OrderEvent, PedidoConversacional, PointsTransaction, TenantProfile, User
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.rewards import recompensas_service
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_commerce_bp = Blueprint("v2_commerce", __name__, url_prefix="/api/v2")


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    body = dict(payload)
    request_id = str(body.get("request_id") or _request_id())
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str, *, retryable: bool = False):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": retryable,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
            "message": message,
        },
        status_code,
    )


def _tenant_slug_from_request(path_slug: str | None = None) -> str:
    return (
        path_slug
        or request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()


def _user_can_access_tenant(user: User, tenant: TenantProfile) -> bool:
    role = str(getattr(user, "rol", "") or "").lower()
    if role == "super_admin":
        return True
    if str(getattr(user, "tenant_id", "") or "") == str(tenant.id):
        return True
    if (getattr(user, "tenant_slug", "") or "").strip().lower() == (tenant.slug or "").strip().lower():
        return True
    owner_ids = {getattr(tenant, "pyme_id", None), getattr(tenant, "municipio_id", None)}
    return getattr(user, "id", None) in owner_ids


def _resolve_tenant_or_error(current_user: User, path_slug: str | None = None):
    slug = _tenant_slug_from_request(path_slug)
    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=slug or None)
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "send_valid_tenant")

    if not _user_can_access_tenant(current_user, tenant):
        return None, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")
    return tenant, None


def _tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "plan": tenant.plan,
    }


def _tenant_config(tenant: TenantProfile) -> dict[str, Any]:
    return tenant.configuracion if isinstance(tenant.configuracion, dict) else {}


def _payment_capabilities(tenant: TenantProfile) -> dict[str, Any]:
    cfg = _tenant_config(tenant)
    gateway = str(cfg.get("payment_gateway") or "mercadopago").strip().lower()
    mercadopago_ready = bool(cfg.get("mercadopago_access_token"))
    rewards_rules = cfg.get("rewards_rules") if isinstance(cfg.get("rewards_rules"), dict) else {}
    missing = []
    if not mercadopago_ready:
        missing.append("mercadopago_access_token")

    gateway_hint = (
        "Mercado Pago configurado para este tenant"
        if mercadopago_ready
        else "Mercado Pago pendiente de configurar para este tenant"
    )
    return {
        "payment_ready": mercadopago_ready,
        "mercadopago_ready": mercadopago_ready,
        "gateway": gateway,
        "gateway_hint": gateway_hint,
        "missing": missing,
        "capabilities": {
            "monetary_checkout": mercadopago_ready,
            "manual_confirmation": True,
            "points_redemption": True,
            "donations": True,
            "post_payment_status": True,
        },
        "checkout_urls": {
            "public_cart_url": f"/{tenant.slug}/carrito" if tenant.slug else None,
            "public_catalog_url": f"/{tenant.slug}/catalogo" if tenant.slug else None,
            "success_url": cfg.get("checkout_success_url"),
            "failure_url": cfg.get("checkout_failure_url"),
            "pending_url": cfg.get("checkout_pending_url"),
        },
        "rewards_rules_configured": bool(rewards_rules),
    }


def _app_base_url() -> str:
    return str(os.getenv("APP_BASE_URL") or "https://chatboc.ar").rstrip("/")


def _as_number(value: Any) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _preview_totals(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    total_monetary = _as_number(payload.get("total_monetary") or payload.get("total_monetario"))
    total_points = _as_int(payload.get("total_points") or payload.get("total_puntos"))
    currency = str(payload.get("currency") or payload.get("moneda") or "ARS").upper()

    normalized_items = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        quantity = max(1, _as_int(raw.get("quantity") or raw.get("cantidad") or 1))
        item_currency = str(raw.get("currency_id") or raw.get("currency") or raw.get("moneda") or currency).upper()
        unit_price = _as_number(raw.get("unit_price") or raw.get("precio_unitario") or raw.get("price") or raw.get("precio"))
        points_price = _as_int(raw.get("points") or raw.get("precio_puntos"))
        modalidad = str(raw.get("modalidad") or "").lower()
        if item_currency == "PTS" or modalidad == "canje" or points_price:
            subtotal_points = int((points_price or unit_price) * quantity)
            total_points += subtotal_points
            subtotal_money = 0.0
        else:
            subtotal_money = round(unit_price * quantity, 2)
            total_monetary += subtotal_money
            subtotal_points = 0
        normalized_items.append(
            {
                "id": raw.get("id") or raw.get("catalogo_item_id"),
                "title": raw.get("title") or raw.get("nombre") or raw.get("name") or "Item",
                "quantity": quantity,
                "currency": item_currency,
                "unit_price": unit_price,
                "subtotal_monetary": subtotal_money,
                "subtotal_points": subtotal_points,
            }
        )

    return {
        "items": normalized_items,
        "total_monetary": round(total_monetary, 2),
        "total_points": int(total_points),
        "currency": currency,
        "items_count": sum(item["quantity"] for item in normalized_items),
    }


def _mercadopago_items(totals: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for item in totals.get("items") or []:
        if float(item.get("subtotal_monetary") or 0) <= 0:
            continue
        quantity = max(1, int(item.get("quantity") or 1))
        unit_price = round(float(item.get("subtotal_monetary") or 0) / quantity, 2)
        items.append(
            {
                "title": str(item.get("title") or "Item")[:250],
                "quantity": quantity,
                "unit_price": unit_price,
                "currency_id": str(item.get("currency") or totals.get("currency") or "ARS").upper(),
            }
        )
    if not items and float(totals.get("total_monetary") or 0) > 0:
        items.append(
            {
                "title": "Checkout Chatboc",
                "quantity": 1,
                "unit_price": round(float(totals.get("total_monetary") or 0), 2),
                "currency_id": str(totals.get("currency") or "ARS").upper(),
            }
        )
    return items


def _payment_status_label(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"pagado", "approved", "paid", "confirmado", "confirmed"}:
        return "paid"
    if normalized in {"rejected", "rechazado", "payment_failed", "failed"}:
        return "failed"
    if normalized in {"cancelled", "canceled", "cancelado"}:
        return "cancelled"
    if normalized in {"pending", "pendiente", "pendiente_pago", "pending_payment", "in_process"}:
        return "pending_payment"
    return normalized or "unknown"


def _find_payment_resources(tenant: TenantProfile, filters: dict[str, Any]) -> tuple[PedidoConversacional | None, MarketOrder | None]:
    pedido = None
    market_order = None
    pedido_id = filters.get("pedido_id") or filters.get("order_id")
    market_order_id = filters.get("market_order_id")
    preference_id = filters.get("preference_id") or filters.get("mp_preference_id")
    external_reference = filters.get("external_reference")

    if pedido_id:
        try:
            pedido = PedidoConversacional.query.filter_by(id=int(pedido_id), tenant_id=tenant.id).first()
        except (TypeError, ValueError):
            pedido = None
    if pedido is None and preference_id:
        pedido = PedidoConversacional.query.filter_by(tenant_id=tenant.id, mp_preference_id=str(preference_id)).first()
    if pedido is None and external_reference:
        try:
            pedido = PedidoConversacional.query.filter_by(id=int(external_reference), tenant_id=tenant.id).first()
        except (TypeError, ValueError):
            pedido = None

    if market_order_id:
        try:
            market_order = MarketOrder.query.filter_by(id=int(market_order_id), tenant_id=tenant.id).first()
        except (TypeError, ValueError):
            market_order = None
    if market_order is None and pedido is not None:
        market_order = MarketOrder.query.filter_by(
            tenant_id=tenant.id,
            external_provider="pedido_conversacional",
            external_order_id=str(pedido.id),
        ).first()
    if market_order is None and external_reference:
        market_order = MarketOrder.query.filter_by(tenant_id=tenant.id, external_order_id=str(external_reference)).first()

    return pedido, market_order


def _payment_status_payload(tenant: TenantProfile, pedido: PedidoConversacional | None, market_order: MarketOrder | None) -> dict[str, Any]:
    pedido_status = getattr(pedido, "estado", None)
    market_status = getattr(market_order, "status", None)
    raw_status = getattr(pedido, "mp_status", None) or pedido_status or market_status
    normalized_status = _payment_status_label(raw_status)
    total_monetary = getattr(pedido, "monto_monetario", None)
    if total_monetary is None and market_order is not None:
        total_monetary = market_order.total_monetary
    total_points = getattr(pedido, "monto_puntos", None)
    if total_points is None and market_order is not None:
        total_points = market_order.total_points

    timeline = []
    if pedido is not None:
        timeline.append(
            {
                "id": f"pedido-{pedido.id}",
                "type": "pedido.status",
                "status": pedido.estado,
                "created_at": pedido.updated_at.isoformat() if pedido.updated_at else None,
            }
        )
    if market_order is not None:
        events_rel = getattr(market_order, "events", None)
        if hasattr(events_rel, "order_by"):
            events = list(reversed(events_rel.order_by(OrderEvent.created_at.desc()).limit(10).all()))
        else:
            events = list(events_rel or [])[-10:]
        for event in events:
            timeline.append(
                {
                    "id": event.id,
                    "type": event.type,
                    "payload": event.payload if isinstance(event.payload, dict) else {},
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                }
            )

    return {
        "contract_version": "payments.status.v1",
        "tenant": _tenant_ref(tenant),
        "payment": {
            "status": normalized_status,
            "paid": normalized_status == "paid",
            "gateway": "mercadopago",
            "mp_status": getattr(pedido, "mp_status", None),
            "mp_payment_id": getattr(pedido, "mp_payment_id", None),
            "preference_id": getattr(pedido, "mp_preference_id", None),
        },
        "order": {
            "pedido_id": getattr(pedido, "id", None),
            "market_order_id": getattr(market_order, "id", None),
            "estado": pedido_status,
            "market_status": market_status,
            "total_monetary": float(total_monetary or 0),
            "total_points": int(total_points or 0),
            "currency": getattr(market_order, "currency", None) or "ARS",
        },
        "timeline": timeline,
    }


def _contact_ready(payload: dict[str, Any], user: User) -> bool:
    contact = payload.get("contact") or payload.get("contacto") or {}
    if not isinstance(contact, dict):
        contact = {}
    return bool(
        contact.get("email")
        or contact.get("telefono")
        or contact.get("phone")
        or payload.get("email")
        or payload.get("telefono")
        or getattr(user, "email", None)
        or getattr(user, "telefono", None)
    )


def _reward_rules(tenant: TenantProfile) -> dict[str, int]:
    cfg = _tenant_config(tenant)
    rules = cfg.get("rewards_rules") if isinstance(cfg.get("rewards_rules"), dict) else {}
    defaults = {"encuesta": 50, "reclamo": 20, "sugerencia": 30, "compra": 10}
    normalized = dict(defaults)
    for key, value in rules.items():
        normalized[str(key)] = _as_int(value)
    return normalized


def _reward_catalog(tenant: TenantProfile) -> list[dict[str, Any]]:
    cfg = _tenant_config(tenant)
    configured = cfg.get("rewards_redemptions") if isinstance(cfg.get("rewards_redemptions"), list) else None
    if configured:
        items = configured
    else:
        items = [
            {"id": "discount_10", "label": "Descuento 10%", "points_cost": 800, "type": "discount"},
            {"id": "priority_support", "label": "Prioridad de atencion", "points_cost": 300, "type": "service"},
            {"id": "free_delivery", "label": "Envio bonificado", "points_cost": 500, "type": "shipping"},
        ]

    catalog = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        points_cost = _as_int(raw.get("points_cost") or raw.get("cost") or raw.get("puntos"))
        reward_id = str(raw.get("id") or raw.get("key") or "").strip()
        if not reward_id or points_cost <= 0:
            continue
        catalog.append(
            {
                "id": reward_id,
                "label": raw.get("label") or raw.get("title") or reward_id,
                "description": raw.get("description") or raw.get("descripcion"),
                "points_cost": points_cost,
                "type": raw.get("type") or raw.get("tipo") or "benefit",
                "status": raw.get("status") or "available",
            }
        )
    return catalog


def _reward_history(user: User, limit: int = 10) -> list[dict[str, Any]]:
    rows = recompensas_service().historial_query(user).limit(limit).all()
    return [
        {
            "id": tx.id,
            "tipo": tx.tipo,
            "delta": tx.delta,
            "saldo_final": tx.saldo_final,
            "metadata": tx.metadata_payload if isinstance(tx.metadata_payload, dict) else {},
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in rows
    ]


def _idempotency_key(payload: dict[str, Any]) -> str | None:
    value = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
    value = str(value or "").strip()
    return value or None


def _existing_redemption(current_user: User, tenant: TenantProfile, key: str | None) -> PointsTransaction | None:
    if not key:
        return None
    rows = (
        PointsTransaction.query.filter_by(user_id=current_user.id, tenant_id=tenant.id, tipo="reward_redeem")
        .order_by(PointsTransaction.created_at.desc())
        .limit(50)
        .all()
    )
    for row in rows:
        metadata = row.metadata_payload if isinstance(row.metadata_payload, dict) else {}
        if metadata.get("idempotency_key") == key:
            return row
    return None


@v2_commerce_bp.route("/payments/checkout-status", methods=["GET"])
@v2_commerce_bp.route("/payments/capabilities", methods=["GET"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/checkout-status", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def payment_checkout_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    capabilities = _payment_capabilities(tenant)
    return _json_response({"contract_version": "payments.checkout_status.v1", "tenant": _tenant_ref(tenant), **capabilities})


@v2_commerce_bp.route("/payments/checkout-preview", methods=["POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/checkout-preview", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def payment_checkout_preview_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    totals = _preview_totals(payload)
    payment = _payment_capabilities(tenant)
    payment_required = totals["total_monetary"] > 0
    contact_ready = _contact_ready(payload, current_user)
    checkout_status = (
        "ready"
        if ((not payment_required or payment["payment_ready"]) and contact_ready)
        else "needs_contact"
        if not contact_ready
        else "needs_gateway"
    )
    next_steps = [
        {"id": "confirm_contact", "label": "Confirmar contacto", "status": "ready" if contact_ready else "required"},
        {
            "id": "complete_payment",
            "label": "Completar pago",
            "status": "ready" if payment_required and payment["payment_ready"] else "not_required" if not payment_required else "pending_configuration",
        },
        {"id": "track_order", "label": "Seguir pedido", "status": "ready"},
    ]
    return _json_response(
        {
            "contract_version": "payments.checkout_preview.v1",
            "tenant": _tenant_ref(tenant),
            "status": checkout_status,
            "summary": totals,
            "payment_required": payment_required,
            "payment_ready": (not payment_required) or payment["payment_ready"],
            "contact_ready": contact_ready,
            "checkout_options": {
                "payment_required": payment_required,
                "payment_ready": (not payment_required) or payment["payment_ready"],
                "requires_contact_or_auth": not contact_ready,
                "gateway": payment["gateway"],
                "gateway_hint": payment["gateway_hint"],
                "mercadopago_ready": payment["mercadopago_ready"],
                "preference_id": None,
                "init_point": None,
            },
            "next_steps": next_steps,
            "idempotency_key": _idempotency_key(payload),
        }
    )


@v2_commerce_bp.route("/payments/checkout-session", methods=["POST"])
@v2_commerce_bp.route("/payments/preference", methods=["POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/checkout-session", methods=["POST"])
@token_requerido
def payment_checkout_session_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    totals = _preview_totals(payload)
    if float(totals.get("total_monetary") or 0) <= 0:
        return _error_response("El checkout no requiere pago monetario", 400, "payment_not_required", "confirm_without_gateway")

    payment = _payment_capabilities(tenant)
    cfg = _tenant_config(tenant)
    access_token = cfg.get("mercadopago_access_token")
    if not access_token:
        return _error_response(
            "Mercado Pago no configurado para este tenant",
            503,
            "payment_gateway_not_configured",
            "configure_mercadopago_access_token",
            retryable=True,
        )

    key = _idempotency_key(payload)
    request_id = _request_id()
    external_reference = str(payload.get("external_reference") or key or f"chk_{uuid.uuid4().hex[:16]}")
    base_url = _app_base_url()
    preference_payload = {
        "items": _mercadopago_items(totals),
        "external_reference": external_reference,
        "metadata": {
            "tenant_id": tenant.id,
            "tenant_slug": tenant.slug,
            "request_id": request_id,
            "idempotency_key": key,
            "source": "api_v2_payments",
        },
        "back_urls": {
            "success": cfg.get("checkout_success_url") or f"{base_url}/{tenant.slug}/checkout/success",
            "failure": cfg.get("checkout_failure_url") or f"{base_url}/{tenant.slug}/checkout/failure",
            "pending": cfg.get("checkout_pending_url") or f"{base_url}/{tenant.slug}/checkout/pending",
        },
        "auto_return": "approved",
    }
    payer_email = ((payload.get("contact") or payload.get("contacto") or {}).get("email") if isinstance(payload.get("contact") or payload.get("contacto"), dict) else None) or payload.get("email")
    if payer_email:
        preference_payload["payer"] = {"email": str(payer_email).strip()}

    try:
        mp_response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=preference_payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException:
        return _error_response(
            "No pudimos crear la preferencia de pago",
            502,
            "payment_gateway_unavailable",
            "retry_checkout_session",
            retryable=True,
        )

    if not getattr(mp_response, "ok", False):
        return _error_response(
            "Mercado Pago rechazo la preferencia",
            502,
            "payment_gateway_rejected",
            "check_payment_payload",
            retryable=True,
        )

    mp_data = mp_response.json() if callable(getattr(mp_response, "json", None)) else {}
    preference_id = mp_data.get("id")
    init_point = mp_data.get("init_point") or mp_data.get("sandbox_init_point")
    return _json_response(
        {
            "ok": True,
            "contract_version": "payments.checkout_session.v1",
            "request_id": request_id,
            "tenant": _tenant_ref(tenant),
            "gateway": payment["gateway"],
            "status": "pending_payment",
            "external_reference": external_reference,
            "preference_id": preference_id,
            "init_point": init_point,
            "sandbox_init_point": mp_data.get("sandbox_init_point"),
            "summary": totals,
            "checkout_options": {
                "payment_required": True,
                "payment_ready": bool(preference_id and init_point),
                "gateway": payment["gateway"],
                "gateway_hint": payment["gateway_hint"],
                "mercadopago_ready": True,
                "preference_id": preference_id,
                "init_point": init_point,
            },
            "idempotency_key": key,
        }
    )


@v2_commerce_bp.route("/payments/status", methods=["GET", "POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/status", methods=["GET", "POST"])
@token_requerido
def payment_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    filters = {
        "pedido_id": request.args.get("pedido_id") or body.get("pedido_id"),
        "order_id": request.args.get("order_id") or body.get("order_id"),
        "market_order_id": request.args.get("market_order_id") or body.get("market_order_id"),
        "preference_id": request.args.get("preference_id") or body.get("preference_id"),
        "mp_preference_id": request.args.get("mp_preference_id") or body.get("mp_preference_id"),
        "external_reference": request.args.get("external_reference") or body.get("external_reference"),
    }
    if not any(filters.values()):
        return _error_response("Se requiere un identificador de pago u orden", 400, "payment_reference_required", "send_payment_reference")

    pedido, market_order = _find_payment_resources(tenant, filters)
    if pedido is None and market_order is None:
        return _error_response("Pago u orden no encontrados", 404, "payment_not_found", "refresh_payment_status")

    return _json_response(_payment_status_payload(tenant, pedido, market_order))


@v2_commerce_bp.route("/rewards/profile", methods=["GET"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/rewards/profile", methods=["GET"])
@token_requerido
def rewards_profile_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    pending_points = _as_int(request.args.get("pending_points") or request.args.get("puntos_en_carrito"))
    balance = recompensas_service().obtener_saldo(current_user)
    catalog = _reward_catalog(tenant)
    return _json_response(
        {
            "contract_version": "rewards.profile.v1",
            "tenant": _tenant_ref(tenant),
            "user": {"id": current_user.id, "name": current_user.name},
            "wallet": {
                "balance": balance,
                "saldo": balance,
                "pending_cart_points": pending_points,
                "estimated_after_cart": max(balance - pending_points, 0),
            },
            "rules": _reward_rules(tenant),
            "available_redemptions": [
                {**item, "redeemable": balance >= int(item.get("points_cost") or 0)}
                for item in catalog
            ],
            "history": _reward_history(current_user),
            "summary": {
                "redemptions_available": len(catalog),
                "redeemable_now": sum(1 for item in catalog if balance >= int(item.get("points_cost") or 0)),
            },
        }
    )


@v2_commerce_bp.route("/rewards/redeem", methods=["POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/rewards/redeem", methods=["POST"])
@token_requerido
def rewards_redeem_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    key = _idempotency_key(payload)
    existing = _existing_redemption(current_user, tenant, key)
    if existing:
        metadata = existing.metadata_payload if isinstance(existing.metadata_payload, dict) else {}
        return _json_response(
            {
                "ok": True,
                "contract_version": "rewards.redeem.v1",
                "tenant": _tenant_ref(tenant),
                "redemption_id": metadata.get("redemption_id") or f"red_{existing.id}",
                "reward_id": metadata.get("reward_id"),
                "status": "redeemed",
                "duplicate": True,
                "balance": existing.saldo_final,
                "idempotency_key": key,
            }
        )

    reward_id = str(payload.get("reward_id") or payload.get("benefit_id") or "").strip()
    catalog = _reward_catalog(tenant)
    reward = next((item for item in catalog if item["id"] == reward_id), None)
    if reward is None:
        return _error_response("Beneficio no encontrado", 404, "reward_not_found", "choose_valid_reward")

    points_cost = int(reward["points_cost"])
    balance = recompensas_service().obtener_saldo(current_user)
    if balance < points_cost:
        return _json_response(
            {
                "contract_version": "shared.error.v1",
                "status_code": 400,
                "reason_code": "insufficient_points",
                "retryable": False,
                "action_hint": "earn_more_points",
                "error": {"code": 400, "message": "Saldo de puntos insuficiente"},
                "message": "Saldo de puntos insuficiente",
                "points_required": points_cost,
                "points_available": balance,
            },
            400,
        )

    redemption_id = f"red_{uuid.uuid4().hex[:12]}"
    ok = recompensas_service().canjear_puntos(
        current_user,
        tenant,
        points_cost,
        tipo="reward_redeem",
        metadata={
            "reward_id": reward_id,
            "reward_label": reward.get("label"),
            "redemption_id": redemption_id,
            "idempotency_key": key,
            "redeemed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if not ok:
        return _error_response("Saldo de puntos insuficiente", 400, "insufficient_points", "earn_more_points")

    db.session.refresh(current_user)
    return _json_response(
        {
            "ok": True,
            "contract_version": "rewards.redeem.v1",
            "tenant": _tenant_ref(tenant),
            "redemption_id": redemption_id,
            "reward_id": reward_id,
            "reward": reward,
            "status": "redeemed",
            "balance": recompensas_service().obtener_saldo(current_user),
            "idempotency_key": key,
        }
    )
