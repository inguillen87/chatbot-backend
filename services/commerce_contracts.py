from __future__ import annotations

import os
from typing import Any

import requests

from services.contact_intake import infer_phone_from_anon_id, normalize_email, normalize_name
from services.plan_access import integration_access_payload


class PaymentGatewayError(Exception):
    def __init__(self, message: str, reason_code: str, action_hint: str, *, status_code: int = 502, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.status_code = status_code
        self.retryable = retryable


def normalize_sales_channel(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "web"
    if "whatsapp" in value:
        return "whatsapp"
    if "widget" in value:
        return "widget"
    if value in {"manual_admin", "manual", "backoffice"}:
        return "manual_admin"
    if "voice" in value or "telefono" in value or "phone" in value or value == "call_center":
        return "phone"
    if value in {"market", "pwa", "web_widget", "site", "ecommerce"}:
        return "web"
    return value


def build_contact_key(
    *,
    user_id: Any = None,
    email: Any = None,
    phone: Any = None,
    anon_id: Any = None,
    session_id: Any = None,
) -> str | None:
    if user_id:
        return f"user:{user_id}"

    email_normalized = normalize_email(email)
    if email_normalized:
        return f"email:{email_normalized}"

    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"phone:{digits}"

    if anon_id:
        return f"anon:{str(anon_id).strip()}"

    if session_id:
        return f"session:{str(session_id).strip()}"

    return None


def build_customer_profile(
    *,
    user: Any = None,
    payload: dict[str, Any] | None = None,
    session_id: Any = None,
    channel: Any = None,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    contact = payload.get("contacto") if isinstance(payload.get("contacto"), dict) else {}

    inferred_phone = infer_phone_from_anon_id(
        payload.get("anon_id")
        or contact.get("anon_id")
        or getattr(user, "anon_id", None)
    )
    raw_phone = (
        contact.get("telefono")
        or contact.get("phone")
        or payload.get("telefono")
        or payload.get("phone")
        or getattr(user, "telefono", None)
        or inferred_phone
    )
    raw_email = contact.get("email") or payload.get("email") or getattr(user, "email", None)
    raw_name = (
        contact.get("nombre")
        or payload.get("nombre")
        or payload.get("name")
        or getattr(user, "name", None)
    )
    anon_id = payload.get("anon_id") or contact.get("anon_id") or getattr(user, "anon_id", None)
    normalized_channel = normalize_sales_channel(channel or payload.get("channel") or payload.get("canal"))
    phone = str(raw_phone).strip() if raw_phone else None
    email = normalize_email(raw_email) or (str(raw_email).strip() if raw_email else None)
    name = normalize_name(raw_name) or (str(raw_name).strip() if raw_name else None)

    return {
        "user_id": getattr(user, "id", None),
        "name": name,
        "email": email,
        "phone": phone,
        "anon_id": anon_id,
        "session_id": str(session_id).strip() if session_id else None,
        "channel": normalized_channel,
        "channel_group": "conversational" if normalized_channel in {"whatsapp", "phone"} else "digital",
        "is_authenticated": bool(getattr(user, "id", None)) and not bool(getattr(user, "anon_id", None)),
        "contact_key": build_contact_key(
            user_id=getattr(user, "id", None),
            email=email,
            phone=phone,
            anon_id=anon_id,
            session_id=session_id,
        ),
    }


def resolve_order_contact_payload(
    *,
    user: Any = None,
    payload: dict[str, Any] | None = None,
    session_id: Any = None,
    channel: Any = None,
) -> dict[str, Any]:
    profile = build_customer_profile(
        user=user,
        payload=payload,
        session_id=session_id,
        channel=channel,
    )
    return {
        "name": profile.get("name"),
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "anon_id": profile.get("anon_id"),
        "channel": profile.get("channel"),
        "contact_key": profile.get("contact_key"),
        "customer_profile": profile,
    }


def tenant_ref(tenant: Any) -> dict[str, Any]:
    return {
        "id": getattr(tenant, "id", None),
        "slug": getattr(tenant, "slug", None),
        "nombre": getattr(tenant, "nombre", None),
        "tipo": getattr(tenant, "tipo", None),
        "plan": getattr(tenant, "plan", None),
    }


def tenant_config(tenant: Any) -> dict[str, Any]:
    return tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}


def build_checkout_experience_payload(
    tenant: Any,
    *,
    channel: Any = None,
    gateway: str | None = None,
    mercadopago_ready: bool | None = None,
) -> dict[str, Any]:
    cfg = tenant_config(tenant)
    active_channel = normalize_sales_channel(channel or "widget")
    access = integration_access_payload(tenant)
    gateway_name = (gateway or str(cfg.get("payment_gateway") or "mercadopago")).strip().lower()
    gateway_configured = bool(cfg.get("mercadopago_access_token")) if mercadopago_ready is None else bool(mercadopago_ready)
    can_checkout = bool(access.get("enabled")) and gateway_configured
    slug = getattr(tenant, "slug", None)

    return {
        "contract_version": "commerce.conversational_checkout_experience.v1",
        "active_entrypoint": active_channel,
        "supported_entrypoints": ["whatsapp", "widget", "web"],
        "mode": "conversation_guided_secure_webview",
        "ready": can_checkout,
        "reason_code": None if can_checkout else ("plan_full_required" if not access.get("enabled") else "payment_gateway_not_configured"),
        "integration_access": access,
        "copy": {
            "title": "Compra y pago por chat",
            "short": "El usuario arma el pedido en WhatsApp o widget y paga en checkout seguro.",
            "customer_ready": "Te dejo el resumen y el link de pago seguro. Cuando se acredite, te aviso por este mismo chat.",
            "customer_pending_gateway": "Ya tengo el pedido. Falta activar el medio de pago del comercio para cobrar online.",
            "customer_locked": "Esta cuenta todavia no tiene habilitado el plan productivo para cobrar desde el chat.",
        },
        "policy": {
            "payment_capture": "external_secure_webview",
            "card_data_in_chat": False,
            "client_return_trusted": False,
            "confirmation_source": "server_to_server_webhook",
            "webhook_required_for_paid_state": True,
            "whatsapp_window_policy": "freeform_inside_24h_template_outside_window",
        },
        "steps": [
            {"id": "collect_contact", "label": "Pedir contacto minimo", "owner": "agent", "required": True},
            {"id": "confirm_cart", "label": "Confirmar carrito y total", "owner": "agent", "required": True},
            {"id": "create_order", "label": "Crear pedido interno", "owner": "backend", "required": True},
            {"id": "open_checkout", "label": "Abrir checkout seguro", "owner": "customer", "required": gateway_configured},
            {"id": "webhook_confirm", "label": "Confirmar pago por webhook", "owner": "backend", "required": True},
            {"id": "notify_and_track", "label": "Enviar comprobante y tracking", "owner": "backend", "required": True},
        ],
        "endpoints": {
            "public_widget_session": "/api/public/widget-commerce-session",
            "public_checkout_session": "/api/checkout/crear-preferencia",
            "admin_checkout_status": f"/api/v2/tenants/{slug}/payments/checkout-status" if slug else "/api/v2/payments/checkout-status",
            "admin_checkout_preview": f"/api/v2/tenants/{slug}/payments/checkout-preview" if slug else "/api/v2/payments/checkout-preview",
            "admin_payment_status": f"/api/v2/tenants/{slug}/payments/status" if slug else "/api/v2/payments/status",
            "public_tracking": "/api/public/tracking/experience?kind=order&code={code}",
        },
        "gateway": {
            "name": gateway_name,
            "configured": gateway_configured,
            "provider_label": "Mercado Pago" if gateway_name == "mercadopago" else gateway_name,
        },
    }


def payment_capabilities(tenant: Any) -> dict[str, Any]:
    cfg = tenant_config(tenant)
    gateway = str(cfg.get("payment_gateway") or "mercadopago").strip().lower()
    mercadopago_ready = bool(cfg.get("mercadopago_access_token"))
    access = integration_access_payload(tenant)
    production_ready = bool(access.get("enabled")) and mercadopago_ready
    rewards_rules = cfg.get("rewards_rules") if isinstance(cfg.get("rewards_rules"), dict) else {}
    missing = []
    if not access.get("enabled"):
        missing.append("plan_full")
    if not mercadopago_ready:
        missing.append("mercadopago_access_token")

    gateway_hint = (
        "Mercado Pago configurado y checkout productivo habilitado"
        if production_ready
        else "Plan Full requerido para cobrar desde WhatsApp o widget"
        if not access.get("enabled")
        else "Mercado Pago pendiente de configurar para este tenant"
        if not mercadopago_ready
        else "Mercado Pago pendiente de configurar para este tenant"
    )
    return {
        "payment_ready": production_ready,
        "gateway_configured": mercadopago_ready,
        "mercadopago_ready": mercadopago_ready,
        "gateway": gateway,
        "gateway_hint": gateway_hint,
        "missing": missing,
        "integration_access": access,
        "capabilities": {
            "monetary_checkout": production_ready,
            "manual_confirmation": True,
            "points_redemption": True,
            "donations": True,
            "post_payment_status": True,
            "whatsapp_checkout": production_ready,
            "widget_checkout": production_ready,
            "conversation_guided_webview": production_ready,
        },
        "checkout_experience": build_checkout_experience_payload(
            tenant,
            channel="widget",
            gateway=gateway,
            mercadopago_ready=mercadopago_ready,
        ),
        "checkout_urls": {
            "public_cart_url": f"/{tenant.slug}/carrito" if getattr(tenant, "slug", None) else None,
            "public_catalog_url": f"/{tenant.slug}/catalogo" if getattr(tenant, "slug", None) else None,
            "success_url": cfg.get("checkout_success_url"),
            "failure_url": cfg.get("checkout_failure_url"),
            "pending_url": cfg.get("checkout_pending_url"),
        },
        "rewards_rules_configured": bool(rewards_rules),
    }


def _as_number(value: Any) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def as_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def normalize_checkout_preview_totals(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    total_monetary = _as_number(payload.get("total_monetary") or payload.get("total_monetario"))
    total_points = as_int(payload.get("total_points") or payload.get("total_puntos"))
    currency = str(payload.get("currency") or payload.get("moneda") or "ARS").upper()

    normalized_items = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        quantity = max(1, as_int(raw.get("quantity") or raw.get("cantidad") or 1))
        item_currency = str(raw.get("currency_id") or raw.get("currency") or raw.get("moneda") or currency).upper()
        unit_price = _as_number(raw.get("unit_price") or raw.get("precio_unitario") or raw.get("price") or raw.get("precio"))
        points_price = as_int(raw.get("points") or raw.get("precio_puntos"))
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


def mercadopago_preference_items(totals: dict[str, Any]) -> list[dict[str, Any]]:
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


def contact_ready_for_checkout(payload: dict[str, Any], user: Any) -> bool:
    payload = payload if isinstance(payload, dict) else {}
    profile = build_customer_profile(user=user, payload=payload)
    return bool(profile.get("email") or profile.get("phone"))


def _app_base_url() -> str:
    return str(os.getenv("APP_BASE_URL") or "https://chatboc.ar").rstrip("/")


def build_mercadopago_preference_payload(
    *,
    tenant: Any,
    totals: dict[str, Any],
    payload: dict[str, Any],
    idempotency_key: str | None,
    request_id: str,
) -> tuple[dict[str, Any], str]:
    cfg = tenant_config(tenant)
    external_reference = str(payload.get("external_reference") or idempotency_key or f"chk_{os.urandom(8).hex()}")
    base_url = _app_base_url()
    preference_payload = {
        "items": mercadopago_preference_items(totals),
        "external_reference": external_reference,
        "metadata": {
            "tenant_id": getattr(tenant, "id", None),
            "tenant_slug": getattr(tenant, "slug", None),
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "source": "api_v2_payments",
        },
        "back_urls": {
            "success": cfg.get("checkout_success_url") or f"{base_url}/{tenant.slug}/checkout/success",
            "failure": cfg.get("checkout_failure_url") or f"{base_url}/{tenant.slug}/checkout/failure",
            "pending": cfg.get("checkout_pending_url") or f"{base_url}/{tenant.slug}/checkout/pending",
        },
        "auto_return": "approved",
    }
    contact = payload.get("contact") or payload.get("contacto") or {}
    payer_email = (contact.get("email") if isinstance(contact, dict) else None) or payload.get("email")
    if payer_email:
        preference_payload["payer"] = {"email": str(payer_email).strip()}
    return preference_payload, external_reference


def create_mercadopago_preference(access_token: str, preference_payload: dict[str, Any]) -> dict[str, Any]:
    try:
        mp_response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=preference_payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise PaymentGatewayError(
            "No pudimos crear la preferencia de pago",
            "payment_gateway_unavailable",
            "retry_checkout_session",
        ) from exc

    if not getattr(mp_response, "ok", False):
        raise PaymentGatewayError(
            "Mercado Pago rechazo la preferencia",
            "payment_gateway_rejected",
            "check_payment_payload",
        )

    return mp_response.json() if callable(getattr(mp_response, "json", None)) else {}


def payment_status_label(status: Any) -> str:
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


def find_payment_resources(tenant: Any, filters: dict[str, Any]):
    from models import MarketOrder, PedidoConversacional

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


def build_payment_status_payload(tenant: Any, pedido: Any = None, market_order: Any = None) -> dict[str, Any]:
    from models import OrderEvent

    pedido_status = getattr(pedido, "estado", None)
    market_status = getattr(market_order, "status", None)
    raw_status = getattr(pedido, "mp_status", None) or pedido_status or market_status
    normalized_status = payment_status_label(raw_status)
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

    customer_messages = {
        "paid": "Pago acreditado. El pedido queda confirmado y listo para seguimiento.",
        "pending_payment": "El pago sigue pendiente. Si ya pagaste, esperamos la confirmacion del proveedor.",
        "failed": "El pago no se pudo confirmar. Podes intentar nuevamente o pedir ayuda.",
        "cancelled": "El pago fue cancelado.",
        "unknown": "Todavia no tenemos una confirmacion final del pago.",
    }
    tracking_href = None
    if getattr(tenant, "slug", None) and market_order is not None:
        tracking_href = f"/{tenant.slug}/portal/pedidos/{market_order.id}"

    return {
        "contract_version": "payments.status.v1",
        "tenant": tenant_ref(tenant),
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
        "customer_experience": {
            "message": customer_messages.get(normalized_status, customer_messages["unknown"]),
            "channels": ["whatsapp", "widget", "web"],
            "next_actions": [
                {
                    "id": "track_order",
                    "label": "Seguir pedido",
                    "status": "ready" if tracking_href else "unavailable",
                    "href": tracking_href,
                },
                {
                    "id": "retry_payment",
                    "label": "Reintentar pago",
                    "status": "ready" if normalized_status in {"failed", "pending_payment"} else "not_required",
                    "href": None,
                },
                {
                    "id": "contact_support",
                    "label": "Pedir ayuda",
                    "status": "ready",
                    "href": None,
                },
            ],
        },
    }
