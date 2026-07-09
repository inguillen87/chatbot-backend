import logging
from typing import List, Optional

import requests
from flask import Blueprint, jsonify, request, session, g
from flask_cors import cross_origin
from sqlalchemy import func

from config import ALLOWED_ORIGINS
from database import db
from models import CatalogoItem, CatalogoModalidad, MarketOrder, MarketOrderItem, OrderEvent, PedidoConversacional, TenantProfile, User
from routes.catalogo import _formatear_producto
from services.commerce_contracts import (
    build_checkout_experience_payload,
    build_customer_profile,
    resolve_order_contact_payload,
)
from services.catalog_inventory import inventory_contract
from services.marketplace_analytics import track_marketplace_event
from services.plan_access import integration_plan_required_payload, plan_allows_full_integrations
from services.rewards import recompensas_service
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user

logger = logging.getLogger(__name__)

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


def _cors_kwargs(methods: list[str]) -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _CORS_ALLOWED_HEADERS,
        "methods": methods,
    }


checkout_bp = Blueprint("checkout_bp", __name__, url_prefix="/api/checkout")
pedidos_checkout_bp = Blueprint("pedidos_checkout_bp", __name__, url_prefix="/api/pedidos")


def _session_cart(tenant_id: int):
    carts = session.get("carritos_pymes", {})
    return carts.get(str(tenant_id)) or carts.get(tenant_id) or []


def _lookup_owner(tenant: TenantProfile) -> Optional[User]:
    return getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)


def _normalize_quantity(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _requested_quantity_fits_inventory(item: dict) -> bool:
    inventory = item.get("inventory") or {}
    stock_quantity = inventory.get("stock_quantity")
    if stock_quantity is None:
        return True
    try:
        return float(item.get("quantity") or item.get("cantidad") or 1) <= float(stock_quantity)
    except (TypeError, ValueError):
        return False


def _checkout_inventory_blockers(items: List[dict]) -> List[dict]:
    blockers: List[dict] = []
    for item in items:
        inventory = item.get("inventory") or {}
        stock_status = inventory.get("stock_status")
        stock_quantity = inventory.get("stock_quantity")
        quantity = _normalize_quantity(item.get("quantity") or item.get("cantidad") or 1)
        if stock_status in {"not_available", "out_of_stock"}:
            reason = "Producto no disponible" if stock_status == "not_available" else "Producto sin stock"
        elif stock_quantity is not None and quantity > float(stock_quantity):
            reason = "Cantidad solicitada mayor al stock disponible"
        else:
            continue
        blockers.append(
            {
                "catalogo_item_id": item.get("catalogo_item_id"),
                "title": item.get("title"),
                "quantity_requested": quantity,
                "stock_quantity": stock_quantity,
                "stock_status": stock_status,
                "reason": reason,
                "inventory": inventory,
            }
        )
    return blockers


def _build_items(cart_entries: List[dict], tenant: TenantProfile, owner: Optional[User]) -> List[dict]:
    items: List[dict] = []
    if not cart_entries:
        return items

    catalog_map: dict[int, CatalogoItem] = {}
    item_ids = [entry.get("catalogo_item_id") for entry in cart_entries if entry.get("catalogo_item_id")]
    if item_ids and owner:
        rows = (
            CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
            .filter(
                CatalogoItem.user_id == owner.id,
                func.coalesce(CatalogoItem.tenant_id, tenant.id) == tenant.id,
                CatalogoItem.id.in_(item_ids),
            )
            .all()
        )
        catalog_map = {row.id: row for row in rows}

    for entry in cart_entries:
        item_id = entry.get("catalogo_item_id")
        cantidad = _normalize_quantity(entry.get("cantidad") or entry.get("quantity"))
        item = catalog_map.get(item_id)
        if not item:
            continue

        formatted = _formatear_producto(
            {
                "nombre": item.nombre,
                "precio_str": item.precio,
                "precio_float": float(item.precio_monetario) if item.precio_monetario is not None else None,
                "sku": item.sku,
                "modalidad": item.modalidad_enum.value,
                "descripcion": item.descripcion,
                "imagen_url": item.imagen_url,
            }
        )
        modalidad = formatted.get("modalidad") or item.modalidad_enum.value
        currency = formatted.get("moneda") or item.moneda or ("PTS" if item.precio_puntos else "ARS")
        unit_price = formatted.get("precio_unitario") or formatted.get("precio_pack")
        if (currency == "PTS" or modalidad == CatalogoModalidad.CANJE.value) and item.precio_puntos is not None:
            unit_price = int(item.precio_puntos)
        elif unit_price in (None, 0) and item.precio_monetario is not None:
            unit_price = float(item.precio_monetario)
        inventory = inventory_contract(
            item.cantidad,
            available=bool(item.disponible),
            source="checkout_catalogo_item",
            updated_at=getattr(item, "timestamp", None),
        )
        amount_validated = _requested_quantity_fits_inventory(
            {"quantity": cantidad, "inventory": inventory}
        )
        items.append(
            {
                "title": formatted.get("nombre"),
                "quantity": cantidad,
                "unit_price": unit_price or 0,
                "currency_id": currency,
                "modalidad": modalidad,
                "catalogo_item_id": item.id,
                "tenant_id": tenant.id,
                "categoria": formatted.get("categoria"),
                "imagen_url": formatted.get("imagen_url"),
                "inventory": inventory,
                "stock_quantity": inventory["stock_quantity"],
                "stock_status": inventory["stock_status"],
                "available_to_sell": inventory["available_to_sell"],
                "can_start_order": inventory["can_start_order"],
                "can_confirm_order": bool(inventory["can_confirm_order"] and amount_validated),
                "amount_validated": amount_validated,
            }
        )
    return items


def _totales(items: List[dict]):
    total_money = 0.0
    total_points = 0
    has_donation = False
    for entry in items:
        moneda = entry.get("moneda") or entry.get("currency_id") or "ARS"
        modalidad = CatalogoModalidad.from_legacy(entry.get("modalidad"))
        subtotal = entry.get("subtotal") or entry.get("precio_unitario") or entry.get("unit_price")
        cantidad = entry.get("cantidad") or entry.get("quantity") or 1
        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            cantidad = 1
        if subtotal is None:
            continue
        subtotal = float(subtotal) * cantidad
        if modalidad is CatalogoModalidad.DONACION:
            has_donation = True
            continue
        if moneda == "PTS" and modalidad is CatalogoModalidad.CANJE:
            total_points += int(subtotal)
        elif moneda != "PTS":
            total_money += subtotal
    return total_money, total_points, has_donation


def _resolve_cart(tenant: TenantProfile, owner: Optional[User], payload: dict) -> List[dict]:
    if payload.get("items"):
        return _build_items(payload.get("items") or [], tenant, owner)
    return _build_items(_session_cart(tenant.id), tenant, owner)


def _ensure_contact(user: User, payload: dict) -> Optional[dict]:
    contacto = payload.get("contacto") or {}
    nombre_contacto = (contacto.get("nombre") or payload.get("nombre") or "").strip()
    email_contacto = (contacto.get("email") or payload.get("email") or "").strip()
    telefono_contacto = (contacto.get("telefono") or payload.get("telefono") or "").strip()

    if not nombre_contacto or not (email_contacto or telefono_contacto):
        return {
            "error": "Datos de contacto requeridos para finalizar la compra",
            "contacto_requerido": True,
        }

    if nombre_contacto:
        user.name = nombre_contacto
    if email_contacto:
        existing_email = User.query.filter(User.email == email_contacto, User.id != user.id).first()
        if not existing_email:
            user.email = email_contacto
    if telefono_contacto:
        user.telefono = telefono_contacto
    return None


def _pedido_tipo(total_money: float, total_points: int, has_donation: bool) -> str:
    if total_money == 0 and total_points == 0 and has_donation:
        return "donacion"
    if total_money > 0 and total_points > 0:
        return "mixto"
    if total_points > 0:
        return "canje"
    if has_donation and total_money > 0:
        return "mixto"
    return "compra"


def _sync_checkout_order_state(pedido: PedidoConversacional, market_order: MarketOrder, pedido_estado: str, *, mp_status: str | None = None) -> None:
    pedido.estado = pedido_estado

    market_status_map = {
        "pendiente_pago": "pending_payment",
        "confirmado": "confirmed",
        "cancelado": "cancelled",
    }
    market_order.status = market_status_map.get(pedido_estado, market_order.status or "pending")

    pedido_metadata = dict(pedido.metadata_payload or {})
    pedido_state = dict(pedido_metadata.get("commercial_state") or {})
    if pedido_estado == "pendiente_pago":
        pedido_state["stage"] = "awaiting_payment"
    elif pedido_estado == "confirmado":
        pedido_state["stage"] = "confirmed"
    elif pedido_estado == "cancelado":
        pedido_state["stage"] = "cancelled"
    pedido_metadata["commercial_state"] = pedido_state
    pedido.metadata_payload = pedido_metadata

    market_metadata = dict(market_order.metadata_payload or {})
    commercial_state = dict(market_metadata.get("commercial_state") or {})
    commercial_state["pedido_estado"] = pedido_estado
    commercial_state["market_status"] = market_order.status
    if mp_status is not None:
        pedido.mp_status = mp_status
        commercial_state["mp_status"] = mp_status
    market_metadata["commercial_state"] = commercial_state
    market_order.metadata_payload = market_metadata


def _support_channels(tenant: TenantProfile, owner: Optional[User], channel: str) -> dict:
    phone = getattr(owner, "telefono", None)
    return {
        "preferred": "whatsapp" if phone else (channel or "web"),
        "whatsapp": f"https://wa.me/{''.join(ch for ch in str(phone or '') if ch.isdigit())}" if phone else None,
        "phone": phone,
        "portal": f"/{tenant.slug}/portal" if getattr(tenant, "slug", None) else None,
    }


def _checkout_next_steps(*, tenant: TenantProfile, owner: Optional[User], market_order: MarketOrder, payment_required: bool, payment_ready: bool, channel: str) -> list[dict]:
    support = _support_channels(tenant, owner, channel)
    steps: list[dict] = [
        {
            "id": "track_order",
            "label": "Seguir pedido",
            "status": "ready",
            "href": f"/{tenant.slug}/portal/pedidos/{market_order.id}" if getattr(tenant, "slug", None) else None,
        }
    ]
    if payment_required:
        steps.append(
            {
                "id": "complete_payment",
                "label": "Completar pago",
                "status": "ready" if payment_ready else "pending_configuration",
                "href": None,
            }
        )
    else:
        steps.append(
            {
                "id": "await_confirmation",
                "label": "Esperar confirmación",
                "status": "ready",
                "href": support.get("portal"),
            }
        )
    if support.get("whatsapp"):
        steps.append(
            {
                "id": "handoff_whatsapp",
                "label": "Seguir por WhatsApp",
                "status": "ready",
                "href": support.get("whatsapp"),
            }
        )
    return steps


def _resolve_tenant_user(payload: dict) -> tuple[TenantProfile, User]:
    tenant_arg = payload.get("tenant_slug") or request.args.get("tenant_slug")
    tenant_arg = tenant_arg or request.args.get("tenant") or request.headers.get("X-Tenant")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id") or payload.get("tenant_id")
    has_hint = bool(tenant_arg or tenant_id or widget_token or request.headers.get("X-Whatsapp-Dst"))
    if not has_hint:
        raise TenantResolutionError("Tenant requerido para checkout")
    try:
        tenant, user, _ = resolve_tenant_and_user(
            tenant_slug=tenant_arg,
            tenant_id=tenant_id,
            widget_token=widget_token,
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            current_user=(getattr(g, "user", None) or getattr(g, "viewer", None)),
        )
    except TenantResolutionError as exc:
        raise
    if not tenant:
        raise TenantResolutionError("Tenant no encontrado")
    return tenant, user


def _track_checkout_event(
    tenant: TenantProfile,
    event_name: str,
    *,
    pedido: PedidoConversacional,
    market_order: MarketOrder,
    total_money: float,
    total_points: int,
    cart_entries: list[dict],
    channel: str,
    session_id: str | None,
    anon_id: str | None,
    user_id: int | None,
    status: str,
    payment_required: bool,
    payment_ready: bool,
    extra: dict | None = None,
) -> None:
    track_marketplace_event(
        tenant,
        event_name,
        {
            "source": "checkout_api",
            "pedido_id": pedido.id,
            "market_order_id": market_order.id,
            "items_count": len(cart_entries),
            "total_monetario": total_money,
            "total_puntos": total_points,
            "order_status": getattr(market_order, "status", None),
            "pedido_estado": getattr(pedido, "estado", None),
            "status": status,
            "payment_required": payment_required,
            "payment_ready": payment_ready,
            **(extra or {}),
        },
        channel=channel,
        session_id=session_id,
        anon_id=anon_id,
        entity_ref=f"market_order:{market_order.id}",
        user_id=user_id,
    )


def _crear_pedido(payload: dict):
    try:
        tenant, user = _resolve_tenant_user(payload)
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc), "codigo": "tenant_no_encontrado"}), 404

    owner = _lookup_owner(tenant)
    if not owner:
        return jsonify({"error": "Catálogo no disponible para este tenant"}), 404

    cart_entries = _resolve_cart(tenant, owner, payload)
    if not cart_entries:
        return jsonify({"error": "Carrito vacío"}), 400

    inventory_blockers = _checkout_inventory_blockers(cart_entries)
    if inventory_blockers:
        return (
            jsonify(
                {
                    "error": "Hay productos que no se pueden confirmar con el stock actual.",
                    "codigo": "INVENTARIO_NO_CONFIRMABLE",
                    "inventory_blockers": inventory_blockers,
                    "frontend_contract": {
                        "contract_version": "checkout.inventory_validation.v1",
                        "render_as": "inventory_resolution_required",
                        "next_action": "open_assisted_order",
                    },
                }
            ),
            409,
        )

    total_money, total_points, has_donation = _totales(cart_entries)
    is_anonymous = bool(getattr(user, "anon_id", None))
    checkout_channel = (
        payload.get("channel")
        or request.headers.get("X-Sales-Channel")
        or request.headers.get("X-Channel")
        or "web"
    )
    checkout_experience = build_checkout_experience_payload(tenant, channel=checkout_channel)
    integration_access = checkout_experience.get("integration_access") or {}

    if total_money > 0 and not plan_allows_full_integrations(tenant):
        return (
            jsonify(
                integration_plan_required_payload(
                    tenant,
                    "mercadopago_checkout",
                    contract_version="checkout.payment_access.v1",
                    render_as="payment_integration_locked",
                    extra={
                        "codigo": "PLAN_FULL_REQUERIDO",
                        "message": "Plan Full requerido para cobrar desde WhatsApp, widget o checkout publico.",
                        "integration_access": integration_access,
                        "checkout_experience": checkout_experience,
                    },
                )
            ),
            403,
        )

    if total_points and is_anonymous:
        return (
            jsonify(
                {
                    "error": "Autenticación requerida para canjear puntos",
                    "codigo": "REQUIERE_LOGIN_PUNTOS",
                    "login_required": True,
                }
            ),
            401,
        )

    rewards = recompensas_service()
    if total_points:
        saldo_actual = rewards.obtener_saldo(user)
        if saldo_actual < total_points:
            faltantes = total_points - saldo_actual
            return (
                jsonify(
                    {
                        "error": "Saldo de puntos insuficiente",
                        "codigo": "SALDO_INSUFICIENTE",
                        "puntos_necesarios": faltantes,
                    }
                ),
                400,
            )
        rewards.canjear_puntos(user, tenant, total_points)

    session_identifier = request.headers.get("X-Chat-Session-Id") or request.headers.get("X-Anon-Id")
    contact = resolve_order_contact_payload(
        user=user,
        payload=payload,
        session_id=session_identifier,
        channel=checkout_channel,
    )
    customer_profile = build_customer_profile(
        user=user,
        payload=payload,
        session_id=session_identifier,
        channel=checkout_channel,
    )

    if is_anonymous:
        contact_error = _ensure_contact(user, payload)
        if contact_error:
            return jsonify(contact_error), 400

    pedido_tipo = _pedido_tipo(total_money, total_points, has_donation)
    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=user.id,
        monto_monetario=total_money,
        monto_puntos=total_points,
        tipo=pedido_tipo,
        items=cart_entries,
        origen=payload.get("origen")
        or request.headers.get("X-Checkout-Origin")
        or request.args.get("origen")
        or contact.get("channel")
        or "web",
        anon_id=getattr(user, "anon_id", None),
    )
    pedido.metadata_payload = {
        "contact_key": contact.get("contact_key"),
        "contacto": {
            "nombre": contact.get("name"),
            "telefono": contact.get("phone"),
            "email": contact.get("email"),
        },
        "customer_profile": customer_profile,
        "commercial_state": {
            "stage": "confirmed" if pedido_tipo == "donacion" else ("awaiting_payment" if total_money > 0 else "awaiting_confirmation"),
            "channel": contact.get("channel") or "web",
            "source": "checkout_api",
        },
    }
    db.session.add(pedido)
    db.session.flush()

    market_order = MarketOrder(
        tenant_id=tenant.id,
        user_id=user.id,
        status="confirmed" if pedido_tipo == "donacion" else ("pending_payment" if total_money > 0 else "confirmed"),
        contact_name=contact.get("name"),
        contact_phone=contact.get("phone"),
        contact_email=contact.get("email"),
        contact_key=contact.get("contact_key"),
        channel=contact.get("channel") or "web",
        session_id=session_identifier,
        total_monetary=total_money or None,
        total_points=total_points or None,
        currency="ARS",
        note=(payload.get("nota") or payload.get("note") or "").strip() or None,
        external_provider="pedido_conversacional",
        external_order_id=str(pedido.id),
        metadata_payload={
            "pedido_conversacional_id": pedido.id,
            "customer_profile": customer_profile,
            "checkout_origin": payload.get("origen") or request.headers.get("X-Checkout-Origin"),
        },
    )
    db.session.add(market_order)
    db.session.flush()
    for item in cart_entries:
        db.session.add(MarketOrderItem(
            order_id=market_order.id,
            product_id=item.get("catalogo_item_id"),
            quantity=_normalize_quantity(item.get("quantity") or item.get("cantidad") or 1),
            price_monetary=item.get("unit_price") if item.get("currency_id") != "PTS" else None,
            price_points=int(item.get("unit_price") or 0) if item.get("currency_id") == "PTS" else None,
            currency=item.get("currency_id") or "ARS",
            modalidad=item.get("modalidad"),
            name_snapshot=item.get("title"),
            extra={
                "tenant_id": tenant.id,
                "categoria": item.get("categoria"),
                "inventory": item.get("inventory"),
                "amount_validated": item.get("amount_validated"),
                "can_confirm_order": item.get("can_confirm_order"),
            },
        ))
    db.session.add(OrderEvent(market_order_id=market_order.id, type="checkout.created", payload={
        "pedido_conversacional_id": pedido.id,
        "channel": contact.get("channel") or "web",
        "contact_key": contact.get("contact_key"),
    }))
    db.session.commit()
    _track_checkout_event(
        tenant,
        "checkout_session_created",
        pedido=pedido,
        market_order=market_order,
        total_money=total_money,
        total_points=total_points,
        cart_entries=cart_entries,
        channel=contact.get("channel") or checkout_channel,
        session_id=session_identifier,
        anon_id=getattr(user, "anon_id", None),
        user_id=getattr(user, "id", None),
        status="created",
        payment_required=total_money > 0,
        payment_ready=total_money == 0,
    )

    tenant_cfg = tenant.configuracion or {}
    access_token = tenant_cfg.get("mercadopago_access_token")
    init_point = None
    preference_id = None
    demo_mode = bool((getattr(g, "token_payload", {}) or {}).get("demo_mode"))
    support_channels = _support_channels(tenant, owner, contact.get("channel") or "web")
    checkout_experience = build_checkout_experience_payload(
        tenant,
        channel=contact.get("channel") or checkout_channel,
        mercadopago_ready=bool(access_token),
    )
    integration_access = checkout_experience.get("integration_access") or {}

    if total_money > 0 and demo_mode:
        _sync_checkout_order_state(pedido, market_order, "confirmado", mp_status="demo_skipped")
        db.session.commit()
        _track_checkout_event(
            tenant,
            "order_created",
            pedido=pedido,
            market_order=market_order,
            total_money=total_money,
            total_points=total_points,
            cart_entries=cart_entries,
            channel=contact.get("channel") or checkout_channel,
            session_id=session_identifier,
            anon_id=getattr(user, "anon_id", None),
            user_id=getattr(user, "id", None),
            status="confirmed_demo",
            payment_required=False,
            payment_ready=True,
            extra={"demo_mode": True},
        )
        return jsonify(
            {
                "pedido_id": pedido.id,
                "market_order_id": market_order.id,
                "preference_id": None,
                "init_point": None,
                "total_monetario": total_money,
                "total_puntos": total_points,
                "estado": pedido.estado,
                "tipo": pedido.tipo,
                "demo_mode": True,
                "integration_access": integration_access,
                "checkout_experience": checkout_experience,
                "tracking": {
                    "market_order_id": market_order.id,
                    "portal_path": f"/{tenant.slug}/portal/pedidos/{market_order.id}",
                    "status_label": "Confirmado",
                },
                "next_steps": _checkout_next_steps(
                    tenant=tenant,
                    owner=owner,
                    market_order=market_order,
                    payment_required=False,
                    payment_ready=False,
                    channel=contact.get("channel") or "web",
                ),
                "support_channels": support_channels,
            }
        )

    if total_money > 0 and not access_token:
        _sync_checkout_order_state(pedido, market_order, "pendiente_pago")
        db.session.commit()
        _track_checkout_event(
            tenant,
            "order_created",
            pedido=pedido,
            market_order=market_order,
            total_money=total_money,
            total_points=total_points,
            cart_entries=cart_entries,
            channel=contact.get("channel") or checkout_channel,
            session_id=session_identifier,
            anon_id=getattr(user, "anon_id", None),
            user_id=getattr(user, "id", None),
            status="pending_payment_missing_provider",
            payment_required=True,
            payment_ready=False,
            extra={"provider": "mercadopago", "provider_ready": False},
        )
        return (
            jsonify(
                {
                    "pedido_id": pedido.id,
                    "market_order_id": market_order.id,
                    "total_monetario": total_money,
                    "total_puntos": total_points,
                    "estado": pedido.estado,
                    "tipo": pedido.tipo,
                    "mercadopago_ready": False,
                    "error": "MercadoPago no configurado para este tenant",
                    "integration_access": integration_access,
                    "checkout_experience": checkout_experience,
                    "tracking": {
                        "market_order_id": market_order.id,
                        "portal_path": f"/{tenant.slug}/portal/pedidos/{market_order.id}",
                        "status_label": "Pendiente de pago",
                    },
                    "next_steps": _checkout_next_steps(
                        tenant=tenant,
                        owner=owner,
                        market_order=market_order,
                        payment_required=True,
                        payment_ready=False,
                        channel=contact.get("channel") or "web",
                    ),
                    "support_channels": support_channels,
                }
            ),
            503,
        )

    if total_money > 0 and access_token:
        preference_payload = {
            "items": [
                {
                    "title": it.get("title"),
                    "quantity": it.get("quantity"),
                    "unit_price": it.get("unit_price"),
                    "currency_id": it.get("currency_id"),
                }
                for it in cart_entries
            ],
            "external_reference": str(pedido.id),
            "metadata": {
                "tenant_id": tenant.id,
                "tenant_slug": tenant.slug,
                "pedido_id": pedido.id,
            },
        }
        resp = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=preference_payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            init_point = data.get("init_point")
            preference_id = data.get("id")
            pedido.mp_preference_id = preference_id
            _sync_checkout_order_state(pedido, market_order, "pendiente_pago")
            db.session.commit()
        else:
            logger.error("MercadoPago error: %s", resp.text)
            _sync_checkout_order_state(pedido, market_order, "pendiente_pago")
            db.session.commit()
    else:
        _sync_checkout_order_state(pedido, market_order, "confirmado")
        db.session.commit()

        # Trigger PymePedido creation for persistence and notifications
        try:
            from services.pedido_service import servicio_pedidos
            servicio_pedidos.create_from_conversational(pedido)
        except Exception as e:
            logger.error(f"Error creating PymePedido from checkout: {e}")

    _track_checkout_event(
        tenant,
        "order_created",
        pedido=pedido,
        market_order=market_order,
        total_money=total_money,
        total_points=total_points,
        cart_entries=cart_entries,
        channel=contact.get("channel") or checkout_channel,
        session_id=session_identifier,
        anon_id=getattr(user, "anon_id", None),
        user_id=getattr(user, "id", None),
        status="pending_payment" if total_money > 0 else "confirmed",
        payment_required=total_money > 0,
        payment_ready=bool(preference_id or total_money == 0),
        extra={"provider": "mercadopago" if total_money > 0 else "internal", "preference_created": bool(preference_id)},
    )
    return jsonify(
        {
            "pedido_id": pedido.id,
            "market_order_id": market_order.id,
            "preference_id": preference_id,
            "init_point": init_point,
            "total_monetario": total_money,
            "total_puntos": total_points,
            "estado": pedido.estado,
            "tipo": pedido.tipo,
            "integration_access": integration_access,
            "checkout_experience": checkout_experience,
            "tracking": {
                "market_order_id": market_order.id,
                "portal_path": f"/{tenant.slug}/portal/pedidos/{market_order.id}",
                "status_label": "Pendiente de pago" if total_money > 0 else "Confirmado",
            },
            "next_steps": _checkout_next_steps(
                tenant=tenant,
                owner=owner,
                market_order=market_order,
                payment_required=total_money > 0,
                payment_ready=bool(preference_id or total_money == 0),
                channel=contact.get("channel") or "web",
            ),
            "support_channels": support_channels,
        }
    )


@checkout_bp.route("/crear-preferencia", methods=["POST", "OPTIONS"])
@pedidos_checkout_bp.route("/checkout", methods=["POST", "OPTIONS"])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
def crear_preferencia():
    if request.method == "OPTIONS":
        return "", 204

    payload = request.get_json(silent=True) or {}
    return _crear_pedido(payload)
