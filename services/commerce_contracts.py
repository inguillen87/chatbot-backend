from __future__ import annotations

import hashlib
import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from services.contact_intake import infer_phone_from_anon_id, normalize_email, normalize_name
from services.plan_access import integration_access_payload
from services.user_service import build_identity_subject


class PaymentGatewayError(Exception):
    def __init__(
        self,
        message: str,
        reason_code: str,
        action_hint: str,
        *,
        status_code: int = 502,
        retryable: bool = True,
        outcome_unknown: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


class CheckoutContractError(Exception):
    def __init__(
        self,
        message: str,
        reason_code: str,
        action_hint: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.status_code = status_code
        self.retryable = retryable
        self.extra = extra or {}


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
    identity = build_identity_subject(
        user=user,
        display_name=name,
        email=email,
        phone=phone,
        anon_id=anon_id,
        source_context="customer_profile",
    )
    identity_visual = {
        key: value
        for key, value in identity.items()
        if key not in {"name", "email", "phone", "user_id", "anon_id"}
    }

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
        "identity": identity,
        **identity_visual,
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


def payment_integration_frontend_contract(access: dict[str, Any] | None = None) -> dict[str, Any]:
    access = access if isinstance(access, dict) else {}
    base_contract = access.get("frontend_contract") if isinstance(access.get("frontend_contract"), dict) else {}
    return {
        **base_contract,
        "render_as": "integration_locked",
        "feature_id": "mercadopago_checkout",
        "primary_action": "upgrade_to_full",
        "hide_payment_credentials_form": True,
        "show_upgrade_cta": True,
        "show_readiness_checklist": True,
        "primary_locked_reason": base_contract.get("primary_locked_reason") or "plan_full_required",
    }


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
    blocking_reasons = []
    if not access.get("enabled"):
        blocking_reasons.append(
            {
                "id": "plan_full_required",
                "label": "Plan productivo requerido",
                "detail": "El tenant necesita un plan con integraciones productivas para cobrar desde WhatsApp o widget.",
                "owner": "tenant_admin",
            }
        )
    if not gateway_configured:
        blocking_reasons.append(
            {
                "id": "payment_gateway_not_configured",
                "label": "Proveedor de pago pendiente",
                "detail": "Configura Mercado Pago para crear links de pago y confirmar acreditaciones por webhook.",
                "owner": "tenant_admin",
            }
        )
    operator_next_actions = [
        {
            "id": "upgrade_plan",
            "label": "Habilitar Plan Full",
            "status": "done" if access.get("enabled") else "required",
        },
        {
            "id": "connect_gateway",
            "label": "Conectar Mercado Pago",
            "status": "done" if gateway_configured else "required",
        },
        {
            "id": "verify_webhook",
            "label": "Verificar webhook de pago",
            "status": "ready" if gateway_configured else "blocked",
        },
    ]

    return {
        "contract_version": "commerce.conversational_checkout_experience.v1",
        "active_entrypoint": active_channel,
        "supported_entrypoints": ["whatsapp", "widget", "web"],
        "mode": "conversation_guided_secure_webview",
        "ready": can_checkout,
        "reason_code": None if can_checkout else ("plan_full_required" if not access.get("enabled") else "payment_gateway_not_configured"),
        "blocking_reasons": blocking_reasons,
        "operator_next_actions": operator_next_actions,
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
        parsed = float(value)
        return max(parsed, 0.0) if math.isfinite(parsed) else 0.0
    except (TypeError, ValueError):
        return 0.0


def as_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _decimal_amount(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise CheckoutContractError(
            f"{field} debe ser numerico",
            "checkout_amount_invalid",
            "send_valid_checkout_amounts",
            status_code=422,
        )
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CheckoutContractError(
            f"{field} debe ser numerico",
            "checkout_amount_invalid",
            "send_valid_checkout_amounts",
            status_code=422,
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise CheckoutContractError(
            f"{field} debe ser finito y no negativo",
            "checkout_amount_invalid",
            "send_valid_checkout_amounts",
            status_code=422,
        )
    return amount


def _money_amount(value: Any, *, field: str) -> Decimal:
    amount = _decimal_amount(value, field=field)
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise CheckoutContractError(
            f"{field} excede el rango monetario admitido",
            "checkout_amount_out_of_range",
            "reduce_checkout_amount",
            status_code=422,
        ) from exc
    if amount != quantized:
        raise CheckoutContractError(
            f"{field} admite como maximo dos decimales",
            "checkout_amount_precision_invalid",
            "send_valid_checkout_amounts",
            status_code=422,
        )
    return quantized


def normalize_checkout_preview_totals(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    declared_monetary = _as_number(_first_present(payload, "total_monetary", "total_monetario"))
    declared_points = as_int(_first_present(payload, "total_points", "total_puntos"))
    currency = str(payload.get("currency") or payload.get("moneda") or "ARS").upper()

    normalized_items = []
    computed_monetary = 0.0
    computed_points = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        quantity = max(1, as_int(_first_present(raw, "quantity", "cantidad") or 1))
        item_currency = str(raw.get("currency_id") or raw.get("currency") or raw.get("moneda") or currency).upper()
        unit_price = _as_number(_first_present(raw, "unit_price", "precio_unitario", "price", "precio"))
        points_price = as_int(_first_present(raw, "points", "precio_puntos"))
        modalidad = str(raw.get("modalidad") or "").lower()
        if item_currency == "PTS" or modalidad == "canje" or points_price:
            subtotal_points = int((points_price or unit_price) * quantity)
            computed_points += subtotal_points
            subtotal_money = 0.0
        else:
            subtotal_money = round(unit_price * quantity, 2)
            computed_monetary += subtotal_money
            subtotal_points = 0
        catalog_item_id = raw.get("catalogo_item_id") or raw.get("catalog_item_id")
        normalized_items.append(
            {
                "id": raw.get("id") or catalog_item_id,
                "catalogo_item_id": catalog_item_id,
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
        "total_monetary": round(computed_monetary if computed_monetary > 0 else declared_monetary, 2),
        "total_points": int(computed_points if computed_points > 0 else declared_points),
        "currency": currency,
        "items_count": sum(item["quantity"] for item in normalized_items),
    }


def validate_checkout_totals(tenant: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CheckoutContractError(
            "El cuerpo del checkout debe ser un objeto JSON",
            "checkout_payload_invalid",
            "send_valid_checkout_payload",
        )

    tenant_id_hint = payload.get("tenant_id") or payload.get("tenantId")
    tenant_slug_hint = payload.get("tenant_slug") or payload.get("tenantSlug")
    if tenant_id_hint is not None and str(tenant_id_hint) != str(getattr(tenant, "id", "")):
        raise CheckoutContractError(
            "El tenant del checkout no coincide con el tenant autenticado",
            "checkout_tenant_mismatch",
            "send_authenticated_tenant",
            status_code=403,
        )
    if tenant_slug_hint and str(tenant_slug_hint).strip().lower() != str(getattr(tenant, "slug", "")).strip().lower():
        raise CheckoutContractError(
            "El tenant del checkout no coincide con el tenant autenticado",
            "checkout_tenant_mismatch",
            "send_authenticated_tenant",
            status_code=403,
        )

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise CheckoutContractError(
            "Se requiere al menos un item para crear la orden de pago",
            "checkout_items_required",
            "send_checkout_items",
            status_code=422,
        )

    monetary_total = Decimal("0")
    points_total = 0
    monetary_currencies: set[str] = set()
    catalog_item_ids: set[int] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise CheckoutContractError(
                f"items[{index}] debe ser un objeto",
                "checkout_item_invalid",
                "send_valid_checkout_items",
                status_code=422,
            )

        item_tenant_id = raw.get("tenant_id") or raw.get("tenantId")
        item_tenant_slug = raw.get("tenant_slug") or raw.get("tenantSlug")
        if item_tenant_id is not None and str(item_tenant_id) != str(getattr(tenant, "id", "")):
            raise CheckoutContractError(
                "Un item pertenece a otro tenant",
                "checkout_item_tenant_mismatch",
                "refresh_tenant_catalog",
                status_code=403,
            )
        if item_tenant_slug and str(item_tenant_slug).strip().lower() != str(getattr(tenant, "slug", "")).strip().lower():
            raise CheckoutContractError(
                "Un item pertenece a otro tenant",
                "checkout_item_tenant_mismatch",
                "refresh_tenant_catalog",
                status_code=403,
            )

        raw_quantity = _first_present(raw, "quantity", "cantidad")
        quantity_amount = _decimal_amount(
            1 if raw_quantity is None else raw_quantity,
            field=f"items[{index}].quantity",
        )
        if quantity_amount != quantity_amount.to_integral_value() or quantity_amount < 1 or quantity_amount > 10000:
            raise CheckoutContractError(
                f"items[{index}].quantity debe ser un entero entre 1 y 10000",
                "checkout_quantity_invalid",
                "send_valid_checkout_items",
                status_code=422,
            )

        item_currency = str(
            raw.get("currency_id")
            or raw.get("currency")
            or raw.get("moneda")
            or payload.get("currency")
            or payload.get("moneda")
            or "ARS"
        ).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", item_currency):
            raise CheckoutContractError(
                f"items[{index}].currency no es valida",
                "checkout_currency_invalid",
                "send_valid_checkout_currency",
                status_code=422,
            )

        unit_price = _decimal_amount(
            _first_present(raw, "unit_price", "precio_unitario", "price", "precio") or 0,
            field=f"items[{index}].unit_price",
        )
        points_price = _decimal_amount(
            _first_present(raw, "points", "precio_puntos") or 0,
            field=f"items[{index}].points",
        )
        modalidad = str(raw.get("modalidad") or "").strip().lower()
        if item_currency == "PTS" or modalidad == "canje" or points_price > 0:
            points_amount = points_price if points_price > 0 else unit_price
            if points_amount != points_amount.to_integral_value():
                raise CheckoutContractError(
                    f"items[{index}] tiene un monto de puntos invalido",
                    "checkout_points_invalid",
                    "send_valid_checkout_items",
                    status_code=422,
                )
            points_total += int(points_amount * quantity_amount)
        else:
            if unit_price <= 0:
                raise CheckoutContractError(
                    f"items[{index}].unit_price debe ser mayor que cero",
                    "checkout_amount_invalid",
                    "send_valid_checkout_amounts",
                    status_code=422,
                )
            unit_price = _money_amount(unit_price, field=f"items[{index}].unit_price")
            monetary_total += unit_price * quantity_amount
            monetary_currencies.add(item_currency)

        catalog_item_id = raw.get("catalogo_item_id") or raw.get("catalog_item_id")
        if catalog_item_id is not None:
            try:
                catalog_item_ids.add(int(catalog_item_id))
            except (TypeError, ValueError) as exc:
                raise CheckoutContractError(
                    f"items[{index}].catalogo_item_id no es valido",
                    "checkout_catalog_item_invalid",
                    "refresh_tenant_catalog",
                    status_code=422,
                ) from exc

    if len(monetary_currencies) > 1:
        raise CheckoutContractError(
            "Todos los items monetarios deben usar la misma moneda",
            "checkout_currency_mismatch",
            "send_single_currency_checkout",
            status_code=422,
        )

    declared_monetary_raw = _first_present(payload, "total_monetary", "total_monetario")
    if declared_monetary_raw is not None:
        declared_monetary = _money_amount(declared_monetary_raw, field="total_monetary")
        if declared_monetary != monetary_total:
            raise CheckoutContractError(
                "El total monetario no coincide con la suma de los items",
                "checkout_total_mismatch",
                "refresh_checkout_totals",
                status_code=422,
                extra={
                    "declared_total_monetary": float(declared_monetary),
                    "computed_total_monetary": float(monetary_total),
                },
            )

    declared_points_raw = _first_present(payload, "total_points", "total_puntos")
    if declared_points_raw is not None:
        declared_points = _decimal_amount(declared_points_raw, field="total_points")
        if declared_points != declared_points.to_integral_value() or int(declared_points) != points_total:
            raise CheckoutContractError(
                "El total de puntos no coincide con la suma de los items",
                "checkout_points_mismatch",
                "refresh_checkout_totals",
                status_code=422,
            )

    if monetary_total <= 0:
        raise CheckoutContractError(
            "El checkout no requiere pago monetario",
            "payment_not_required",
            "confirm_without_gateway",
        )
    if monetary_total > Decimal("9999999999.99"):
        raise CheckoutContractError(
            "El total monetario excede el maximo admitido",
            "checkout_amount_out_of_range",
            "reduce_checkout_amount",
            status_code=422,
        )

    payload_currency = str(payload.get("currency") or payload.get("moneda") or next(iter(monetary_currencies), "ARS")).strip().upper()
    if monetary_currencies and payload_currency not in monetary_currencies:
        raise CheckoutContractError(
            "La moneda del checkout no coincide con la moneda de los items",
            "checkout_currency_mismatch",
            "send_single_currency_checkout",
            status_code=422,
        )

    if catalog_item_ids:
        from models import CatalogoItem
        from services.common_utils import parse_precio_flexible

        rows = CatalogoItem.query.filter(CatalogoItem.id.in_(catalog_item_ids)).all()
        rows_by_id = {row.id: row for row in rows}
        owner_ids = {
            getattr(tenant, "pyme_id", None),
            getattr(tenant, "municipio_id", None),
        }
        valid_ids = {
            row.id
            for row in rows
            if str(getattr(row, "tenant_id", "") or "") == str(getattr(tenant, "id", ""))
            or (getattr(row, "tenant_id", None) is None and getattr(row, "user_id", None) in owner_ids)
        }
        if valid_ids != catalog_item_ids:
            raise CheckoutContractError(
                "Uno o mas items no pertenecen al catalogo del tenant",
                "checkout_catalog_tenant_mismatch",
                "refresh_tenant_catalog",
                status_code=403,
                extra={"invalid_catalog_item_ids": sorted(catalog_item_ids - valid_ids)},
            )

        for index, raw in enumerate(raw_items):
            catalog_item_id = raw.get("catalogo_item_id") or raw.get("catalog_item_id")
            if catalog_item_id is None:
                continue
            catalog_item = rows_by_id[int(catalog_item_id)]
            requested_currency = str(
                raw.get("currency_id")
                or raw.get("currency")
                or raw.get("moneda")
                or payload.get("currency")
                or payload.get("moneda")
                or "ARS"
            ).strip().upper()
            requested_unit_price = _decimal_amount(
                _first_present(raw, "unit_price", "precio_unitario", "price", "precio") or 0,
                field=f"items[{index}].unit_price",
            )
            requested_points = _decimal_amount(
                _first_present(raw, "points", "precio_puntos") or 0,
                field=f"items[{index}].points",
            )
            catalog_points = int(getattr(catalog_item, "precio_puntos", None) or 0)
            catalog_mode = str(getattr(catalog_item, "modalidad", "") or "").strip().lower()
            if requested_currency == "PTS" or catalog_mode == "canje" or catalog_points > 0:
                requested_points = requested_points if requested_points > 0 else requested_unit_price
                if requested_points != Decimal(catalog_points):
                    raise CheckoutContractError(
                        "El precio en puntos no coincide con el catalogo del tenant",
                        "checkout_catalog_amount_mismatch",
                        "refresh_tenant_catalog",
                        status_code=409,
                        extra={"catalogo_item_id": catalog_item.id},
                    )
                continue

            _, catalog_price, catalog_currency = parse_precio_flexible(str(catalog_item.precio or ""))
            if catalog_price is None:
                raise CheckoutContractError(
                    "No se pudo validar el precio del item contra el catalogo",
                    "checkout_catalog_amount_unverifiable",
                    "refresh_tenant_catalog",
                    status_code=409,
                    extra={"catalogo_item_id": catalog_item.id},
                )
            expected_unit_price = _money_amount(
                catalog_price,
                field=f"catalogo[{catalog_item.id}].precio",
            )
            requested_unit_price = _money_amount(
                requested_unit_price,
                field=f"items[{index}].unit_price",
            )
            if requested_unit_price != expected_unit_price:
                raise CheckoutContractError(
                    "El precio del item no coincide con el catalogo del tenant",
                    "checkout_catalog_amount_mismatch",
                    "refresh_tenant_catalog",
                    status_code=409,
                    extra={
                        "catalogo_item_id": catalog_item.id,
                        "requested_unit_price": float(requested_unit_price),
                        "expected_unit_price": float(expected_unit_price),
                    },
                )
            if catalog_currency and requested_currency != str(catalog_currency).upper():
                raise CheckoutContractError(
                    "La moneda del item no coincide con el catalogo del tenant",
                    "checkout_catalog_currency_mismatch",
                    "refresh_tenant_catalog",
                    status_code=409,
                    extra={"catalogo_item_id": catalog_item.id},
                )

    totals = normalize_checkout_preview_totals(payload)
    totals["total_monetary"] = float(monetary_total.quantize(Decimal("0.01")))
    totals["total_points"] = points_total
    totals["currency"] = payload_currency
    return totals


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


_CHECKOUT_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


def normalize_checkout_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        raise CheckoutContractError(
            "Idempotency-Key es requerido para crear una sesion de pago",
            "idempotency_key_required",
            "send_idempotency_key",
        )
    if not _CHECKOUT_IDEMPOTENCY_RE.fullmatch(key):
        raise CheckoutContractError(
            "Idempotency-Key debe tener hasta 128 caracteres seguros",
            "idempotency_key_invalid",
            "send_valid_idempotency_key",
            status_code=422,
        )
    return key


def _checkout_idempotency_storage_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"idem:{digest}"


def _checkout_request_fingerprint(tenant: Any, totals: dict[str, Any]) -> str:
    fingerprint_items = []
    for item in totals.get("items") or []:
        fingerprint_items.append(
            {
                "catalogo_item_id": item.get("catalogo_item_id"),
                "title": str(item.get("title") or "Item")[:250],
                "quantity": int(item.get("quantity") or 1),
                "currency": str(item.get("currency") or totals.get("currency") or "ARS").upper(),
                "unit_price": f"{Decimal(str(item.get('unit_price') or 0)).quantize(Decimal('0.01'))}",
                "subtotal_points": int(item.get("subtotal_points") or 0),
            }
        )
    canonical = {
        "tenant_id": getattr(tenant, "id", None),
        "currency": str(totals.get("currency") or "ARS").upper(),
        "total_monetary": f"{Decimal(str(totals.get('total_monetary') or 0)).quantize(Decimal('0.01'))}",
        "total_points": int(totals.get("total_points") or 0),
        "items": fingerprint_items,
    }
    raw = json.dumps(canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _checkout_metadata(entity: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    metadata = dict(getattr(entity, "metadata_payload", None) or {})
    checkout = dict(metadata.get("checkout") or {})
    payment = dict(checkout.get("payment") or {})
    return metadata, checkout, payment


def _assign_checkout_payment(entity: Any, updates: dict[str, Any]) -> None:
    metadata, checkout, payment = _checkout_metadata(entity)
    payment.update(updates)
    checkout["payment"] = payment
    metadata["checkout"] = checkout
    entity.metadata_payload = metadata


def checkout_order_snapshot(pedido: Any, market_order: Any) -> dict[str, Any]:
    _, checkout, payment = _checkout_metadata(pedido)
    if not payment:
        _, market_checkout, payment = _checkout_metadata(market_order)
        checkout = checkout or market_checkout
    return {
        "pedido_id": getattr(pedido, "id", None),
        "market_order_id": getattr(market_order, "id", None),
        "external_reference": str(getattr(pedido, "id", "")) or None,
        "client_external_reference": checkout.get("client_external_reference"),
        "preference_state": payment.get("state") or "unknown",
        "preference_id": payment.get("preference_id") or getattr(pedido, "mp_preference_id", None),
        "init_point": payment.get("init_point") or getattr(market_order, "external_url", None),
        "sandbox_init_point": payment.get("sandbox_init_point"),
        "preference_attempts": int(payment.get("attempts") or 0),
    }


def reserve_checkout_order(
    *,
    tenant: Any,
    user: Any,
    payload: dict[str, Any],
    totals: dict[str, Any],
    idempotency_key: str,
    request_id: str,
) -> dict[str, Any]:
    from extensions import db
    from models import MarketOrder, MarketOrderItem, OrderEvent, PedidoConversacional, TenantProfile

    fingerprint = _checkout_request_fingerprint(tenant, totals)
    storage_key = _checkout_idempotency_storage_key(idempotency_key)
    try:
        TenantProfile.query.filter_by(id=tenant.id).with_for_update().one()
        existing_market_order = (
            MarketOrder.legacy_safe_query()
            .filter_by(
                tenant_id=tenant.id,
                external_provider="api_v2_checkout",
                external_order_id=storage_key,
            )
            .with_for_update()
            .first()
        )
        if existing_market_order is not None:
            metadata, checkout, payment = _checkout_metadata(existing_market_order)
            if checkout.get("idempotency_key") != idempotency_key or checkout.get("request_fingerprint") != fingerprint:
                db.session.rollback()
                raise CheckoutContractError(
                    "Idempotency-Key ya fue usado con otra orden o monto",
                    "idempotency_key_conflict",
                    "send_new_idempotency_key",
                    status_code=409,
                )

            pedido_id = checkout.get("pedido_conversacional_id") or metadata.get("pedido_conversacional_id")
            pedido = db.session.get(PedidoConversacional, pedido_id) if pedido_id else None
            if pedido is None or str(pedido.tenant_id) != str(tenant.id):
                db.session.rollback()
                raise CheckoutContractError(
                    "La reserva idempotente no tiene una orden reconciliable",
                    "checkout_order_reconciliation_required",
                    "reconcile_checkout_order",
                    status_code=409,
                    extra={"market_order_id": existing_market_order.id},
                )

            snapshot = checkout_order_snapshot(pedido, existing_market_order)
            if snapshot.get("preference_id") and snapshot.get("init_point"):
                db.session.commit()
                return {
                    "pedido": pedido,
                    "market_order": existing_market_order,
                    "duplicate": True,
                    "should_create_preference": False,
                    "snapshot": snapshot,
                }

            state = str(payment.get("state") or "").lower()
            if state in {"creating", "creation_uncertain", "ready"}:
                db.session.rollback()
                raise CheckoutContractError(
                    "La preferencia ya esta en creacion o requiere reconciliacion",
                    "payment_preference_reconciliation_required",
                    "refresh_payment_status",
                    status_code=409,
                    retryable=True,
                    extra=snapshot,
                )

            attempts = int(payment.get("attempts") or 0) + 1
            _assign_checkout_payment(
                pedido,
                {"state": "creating", "attempts": attempts, "request_id": request_id},
            )
            _assign_checkout_payment(
                existing_market_order,
                {"state": "creating", "attempts": attempts, "request_id": request_id},
            )
            db.session.add(
                OrderEvent(
                    market_order_id=existing_market_order.id,
                    type="payment.preference_retry_reserved",
                    payload={"pedido_id": pedido.id, "request_id": request_id, "attempt": attempts},
                )
            )
            db.session.commit()
            return {
                "pedido": pedido,
                "market_order": existing_market_order,
                "duplicate": True,
                "should_create_preference": True,
                "snapshot": checkout_order_snapshot(pedido, existing_market_order),
            }

        channel = normalize_sales_channel(
            payload.get("channel") or payload.get("canal") or payload.get("origen") or "web"
        )
        session_id = payload.get("session_id") or payload.get("chat_session_id")
        contact = resolve_order_contact_payload(
            user=user,
            payload=payload,
            session_id=session_id,
            channel=channel,
        )
        customer_profile = build_customer_profile(
            user=user,
            payload=payload,
            session_id=session_id,
            channel=channel,
        )
        client_external_reference = str(payload.get("external_reference") or "").strip() or None
        checkout_metadata = {
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "request_id": request_id,
            "client_external_reference": client_external_reference,
            "source": "api_v2_payments",
            "payment": {
                "gateway": "mercadopago",
                "state": "creating",
                "attempts": 1,
                "request_id": request_id,
            },
        }
        pedido = PedidoConversacional(
            tenant_id=tenant.id,
            user_id=getattr(user, "id", None),
            estado="pendiente_pago",
            monto_monetario=totals.get("total_monetary"),
            monto_puntos=totals.get("total_points") or 0,
            tipo="mixto" if totals.get("total_points") else "compra",
            origen=channel,
            anon_id=getattr(user, "anon_id", None),
            items=[
                {
                    "title": item.get("title"),
                    "quantity": item.get("quantity"),
                    "unit_price": item.get("unit_price"),
                    "currency_id": item.get("currency"),
                    "catalogo_item_id": item.get("catalogo_item_id"),
                }
                for item in totals.get("items") or []
            ],
            metadata_payload={
                "contract_version": "payments.checkout_order.v1",
                "checkout": checkout_metadata,
                "contact_key": contact.get("contact_key"),
                "contacto": {
                    "nombre": contact.get("name"),
                    "telefono": contact.get("phone"),
                    "email": contact.get("email"),
                },
                "customer_profile": customer_profile,
                "currency": totals.get("currency"),
                "commercial_state": {"stage": "awaiting_payment", "channel": channel},
            },
        )
        db.session.add(pedido)
        db.session.flush()

        market_order = MarketOrder(
            tenant_id=tenant.id,
            user_id=getattr(user, "id", None),
            status="pending_payment",
            contact_name=contact.get("name"),
            contact_phone=contact.get("phone"),
            channel=channel,
            total_monetary=totals.get("total_monetary"),
            total_points=totals.get("total_points") or 0,
            currency=totals.get("currency"),
            note=str(payload.get("note") or payload.get("nota") or "").strip() or None,
            external_provider="api_v2_checkout",
            external_order_id=storage_key,
            metadata_payload={
                "contract_version": "payments.checkout_order.v1",
                "pedido_conversacional_id": pedido.id,
                "checkout": {**checkout_metadata, "pedido_conversacional_id": pedido.id},
                "customer_profile": customer_profile,
            },
        )
        db.session.add(market_order)
        db.session.flush()

        for item in totals.get("items") or []:
            monetary = float(item.get("subtotal_monetary") or 0) > 0
            db.session.add(
                MarketOrderItem(
                    order_id=market_order.id,
                    product_id=item.get("catalogo_item_id"),
                    quantity=int(item.get("quantity") or 1),
                    price_monetary=item.get("unit_price") if monetary else None,
                    price_points=int(item.get("unit_price") or 0) if not monetary else None,
                    currency=item.get("currency"),
                    modalidad="venta" if monetary else "canje",
                    name_snapshot=str(item.get("title") or "Item")[:255],
                    extra={"tenant_id": tenant.id, "request_id": request_id},
                )
            )

        pedido_metadata = dict(pedido.metadata_payload or {})
        pedido_checkout = dict(pedido_metadata.get("checkout") or {})
        pedido_checkout["market_order_id"] = market_order.id
        pedido_checkout["external_reference"] = str(pedido.id)
        pedido_metadata["checkout"] = pedido_checkout
        pedido.metadata_payload = pedido_metadata
        db.session.add(
            OrderEvent(
                market_order_id=market_order.id,
                type="payment.checkout_reserved",
                payload={
                    "pedido_id": pedido.id,
                    "external_reference": str(pedido.id),
                    "request_id": request_id,
                    "total_monetary": totals.get("total_monetary"),
                    "currency": totals.get("currency"),
                },
            )
        )
        db.session.commit()
        return {
            "pedido": pedido,
            "market_order": market_order,
            "duplicate": False,
            "should_create_preference": True,
            "snapshot": checkout_order_snapshot(pedido, market_order),
        }
    except CheckoutContractError:
        raise
    except Exception as exc:
        db.session.rollback()
        raise CheckoutContractError(
            "No pudimos persistir la orden antes de iniciar el pago",
            "checkout_order_persistence_failed",
            "retry_checkout_session",
            status_code=500,
            retryable=True,
        ) from exc


def mark_checkout_preference_ready(
    pedido: Any,
    market_order: Any,
    mp_data: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    from extensions import db
    from models import OrderEvent

    preference_id = str(mp_data.get("id") or "").strip()
    init_point = str(mp_data.get("init_point") or mp_data.get("sandbox_init_point") or "").strip()
    if not preference_id or not init_point:
        raise PaymentGatewayError(
            "Mercado Pago devolvio una preferencia incompleta",
            "payment_gateway_invalid_response",
            "reconcile_checkout_order",
            retryable=False,
            outcome_unknown=True,
        )
    updates = {
        "state": "ready",
        "preference_id": preference_id,
        "init_point": init_point,
        "sandbox_init_point": mp_data.get("sandbox_init_point"),
        "request_id": request_id,
    }
    try:
        pedido.mp_preference_id = preference_id
        pedido.estado = "pendiente_pago"
        market_order.status = "pending_payment"
        market_order.external_url = init_point
        _assign_checkout_payment(pedido, updates)
        _assign_checkout_payment(market_order, updates)
        db.session.add(
            OrderEvent(
                market_order_id=market_order.id,
                type="payment.preference_created",
                payload={
                    "pedido_id": pedido.id,
                    "preference_id": preference_id,
                    "request_id": request_id,
                },
            )
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise CheckoutContractError(
            "La preferencia fue creada pero requiere reconciliacion local",
            "payment_preference_reconciliation_required",
            "reconcile_checkout_order",
            status_code=500,
            retryable=True,
            extra={
                "pedido_id": getattr(pedido, "id", None),
                "market_order_id": getattr(market_order, "id", None),
                "preference_id": preference_id,
            },
        ) from exc
    return checkout_order_snapshot(pedido, market_order)


def mark_checkout_preference_failed(
    pedido: Any,
    market_order: Any,
    error: PaymentGatewayError,
    *,
    request_id: str,
) -> dict[str, Any]:
    from extensions import db
    from models import OrderEvent

    state = "creation_uncertain" if error.outcome_unknown else "failed"
    updates = {
        "state": state,
        "request_id": request_id,
        "last_error": {
            "reason_code": error.reason_code,
            "message": error.message,
            "outcome_unknown": error.outcome_unknown,
        },
    }
    try:
        _assign_checkout_payment(pedido, updates)
        _assign_checkout_payment(market_order, updates)
        db.session.add(
            OrderEvent(
                market_order_id=market_order.id,
                type="payment.preference_failed",
                payload={
                    "pedido_id": pedido.id,
                    "request_id": request_id,
                    "reason_code": error.reason_code,
                    "outcome_unknown": error.outcome_unknown,
                },
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
    return checkout_order_snapshot(pedido, market_order)


def _app_base_url() -> str:
    return str(os.getenv("APP_BASE_URL") or "https://chatboc.ar").rstrip("/")


def build_mercadopago_preference_payload(
    *,
    tenant: Any,
    totals: dict[str, Any],
    payload: dict[str, Any],
    idempotency_key: str | None,
    request_id: str,
    external_reference: str | None = None,
    pedido_id: int | None = None,
    market_order_id: int | None = None,
) -> tuple[dict[str, Any], str]:
    cfg = tenant_config(tenant)
    external_reference = str(external_reference or "").strip()
    if not external_reference:
        raise CheckoutContractError(
            "La preferencia requiere una referencia de orden persistida",
            "checkout_order_reference_required",
            "persist_checkout_order",
            status_code=500,
        )
    base_url = _app_base_url()
    notification_url = cfg.get("mercadopago_notification_url") or (
        f"{base_url}/mercadopago_webhook?tenant_slug={getattr(tenant, 'slug', '')}"
    )
    preference_payload = {
        "items": mercadopago_preference_items(totals),
        "external_reference": external_reference,
        "metadata": {
            "tenant_id": getattr(tenant, "id", None),
            "tenant_slug": getattr(tenant, "slug", None),
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "pedido_id": pedido_id,
            "market_order_id": market_order_id,
            "total_monetary": totals.get("total_monetary"),
            "currency": totals.get("currency"),
            "source": "api_v2_payments",
        },
        "back_urls": {
            "success": cfg.get("checkout_success_url") or f"{base_url}/{tenant.slug}/checkout/success",
            "failure": cfg.get("checkout_failure_url") or f"{base_url}/{tenant.slug}/checkout/failure",
            "pending": cfg.get("checkout_pending_url") or f"{base_url}/{tenant.slug}/checkout/pending",
        },
        "auto_return": "approved",
        "notification_url": notification_url,
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
            "reconcile_checkout_order",
            retryable=False,
            outcome_unknown=True,
        ) from exc

    if not getattr(mp_response, "ok", False):
        gateway_status = int(getattr(mp_response, "status_code", 502) or 502)
        outcome_unknown = gateway_status >= 500
        raise PaymentGatewayError(
            "Mercado Pago rechazo la preferencia",
            "payment_gateway_rejected",
            "reconcile_checkout_order" if outcome_unknown else "check_payment_payload",
            retryable=not outcome_unknown,
            outcome_unknown=outcome_unknown,
        )

    try:
        data = mp_response.json() if callable(getattr(mp_response, "json", None)) else {}
    except (TypeError, ValueError) as exc:
        raise PaymentGatewayError(
            "Mercado Pago devolvio una respuesta invalida",
            "payment_gateway_invalid_response",
            "reconcile_checkout_order",
            retryable=False,
            outcome_unknown=True,
        ) from exc
    if not isinstance(data, dict):
        raise PaymentGatewayError(
            "Mercado Pago devolvio una respuesta invalida",
            "payment_gateway_invalid_response",
            "reconcile_checkout_order",
            retryable=False,
            outcome_unknown=True,
        )
    return data


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
    idempotency_key = filters.get("idempotency_key")

    normalized_external_reference = str(external_reference or "").strip()
    if normalized_external_reference.upper().startswith("PC-"):
        normalized_external_reference = normalized_external_reference[3:]

    if pedido_id:
        try:
            pedido = PedidoConversacional.query.filter_by(id=int(pedido_id), tenant_id=tenant.id).first()
        except (TypeError, ValueError):
            pedido = None
    if pedido is None and preference_id:
        pedido = PedidoConversacional.query.filter_by(tenant_id=tenant.id, mp_preference_id=str(preference_id)).first()
    if pedido is None and normalized_external_reference:
        try:
            pedido = PedidoConversacional.query.filter_by(
                id=int(normalized_external_reference),
                tenant_id=tenant.id,
            ).first()
        except (TypeError, ValueError):
            pedido = None

    if market_order_id:
        try:
            market_order = MarketOrder.query.filter_by(id=int(market_order_id), tenant_id=tenant.id).first()
        except (TypeError, ValueError):
            market_order = None
    if market_order is None and normalized_external_reference.upper().startswith("MO-"):
        try:
            market_order = MarketOrder.legacy_safe_query().filter_by(
                id=int(normalized_external_reference[3:]),
                tenant_id=tenant.id,
            ).first()
        except (TypeError, ValueError):
            market_order = None
    lookup_idempotency_key = idempotency_key
    if lookup_idempotency_key is None and normalized_external_reference and pedido is None and market_order is None:
        lookup_idempotency_key = normalized_external_reference
    if market_order is None and lookup_idempotency_key:
        market_order = MarketOrder.legacy_safe_query().filter_by(
            tenant_id=tenant.id,
            external_provider="api_v2_checkout",
            external_order_id=_checkout_idempotency_storage_key(str(lookup_idempotency_key)),
        ).first()
    if market_order is None and pedido is not None:
        pedido_metadata = dict(getattr(pedido, "metadata_payload", None) or {})
        checkout = dict(pedido_metadata.get("checkout") or {})
        linked_market_order_id = checkout.get("market_order_id")
        if linked_market_order_id:
            market_order = MarketOrder.legacy_safe_query().filter_by(
                id=linked_market_order_id,
                tenant_id=tenant.id,
            ).first()
        if market_order is None:
            market_order = MarketOrder.legacy_safe_query().filter_by(
                tenant_id=tenant.id,
                external_provider="pedido_conversacional",
                external_order_id=str(pedido.id),
            ).first()
    if market_order is None and normalized_external_reference:
        market_order = MarketOrder.legacy_safe_query().filter_by(
            tenant_id=tenant.id,
            external_order_id=normalized_external_reference,
        ).first()

    if pedido is None and market_order is not None:
        market_metadata = dict(getattr(market_order, "metadata_payload", None) or {})
        checkout = dict(market_metadata.get("checkout") or {})
        linked_pedido_id = checkout.get("pedido_conversacional_id") or market_metadata.get("pedido_conversacional_id")
        if linked_pedido_id:
            pedido = PedidoConversacional.query.filter_by(
                id=linked_pedido_id,
                tenant_id=tenant.id,
            ).first()

    return pedido, market_order


def build_payment_status_payload(tenant: Any, pedido: Any = None, market_order: Any = None) -> dict[str, Any]:
    from models import OrderEvent

    pedido_status = getattr(pedido, "estado", None)
    market_status = getattr(market_order, "status", None)
    _, pedido_checkout, pedido_payment = _checkout_metadata(pedido)
    market_metadata, market_checkout, market_payment = _checkout_metadata(market_order)
    payment_metadata = pedido_payment or market_payment
    mp_status = (
        getattr(pedido, "mp_status", None)
        or payment_metadata.get("mp_status")
        or market_metadata.get("mp_status")
    )
    mp_payment_id = (
        getattr(pedido, "mp_payment_id", None)
        or payment_metadata.get("mp_payment_id")
        or market_metadata.get("mp_payment_id")
    )
    preference_id = (
        getattr(pedido, "mp_preference_id", None)
        or payment_metadata.get("preference_id")
    )
    raw_status = mp_status or pedido_status or market_status
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
            "mp_status": mp_status,
            "mp_payment_id": mp_payment_id,
            "preference_id": preference_id,
            "external_reference": str(getattr(pedido, "id", "")) or None,
        },
        "order": {
            "pedido_id": getattr(pedido, "id", None),
            "market_order_id": getattr(market_order, "id", None),
            "estado": pedido_status,
            "market_status": market_status,
            "total_monetary": float(total_monetary or 0),
            "total_points": int(total_points or 0),
            "currency": getattr(market_order, "currency", None) or "ARS",
            "idempotency_key": pedido_checkout.get("idempotency_key") or market_checkout.get("idempotency_key"),
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
