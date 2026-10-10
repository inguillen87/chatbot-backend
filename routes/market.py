from __future__ import annotations

import os
import json
import re
import requests
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from flask import Blueprint, abort, current_app, jsonify, make_response, request, g, session
from flask_login import current_user
from sqlalchemy import func, or_
from werkzeug.exceptions import RequestEntityTooLarge

from cutover_writer_fence import cutover_writer_view
from database import db
from models import (
    CatalogoItem,
    MarketCart,
    MarketCartItem,
    MarketOrder,
    MarketOrderItem,
    OrderEvent,
    TenantProfile,
    User,
)
from routes.catalogo import _formatear_producto
from routes.pwa_public import _build_public_cart_url
from services.catalog_seed import ensure_seed_catalog
from services.commerce_contracts import build_customer_profile, normalize_sales_channel, resolve_order_contact_payload
from services.common_utils import parse_precio_flexible
from services.public_market_catalog import build_public_market_catalog_contract
from services.promocion_service import promocion_service
from services.rewards_demo import reward_profile_for_tenant
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
from services.notification_dispatcher import dispatch_order_update
from utils.auth_helpers import auth_tenant_for_user, token_requerido
from utils.permissions import require_role
from utils.upload_limits import set_upload_request_limit
from utils.roles import (
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    canonical_role,
    is_authorized_superadmin_user,
)
from socket_service import emit_tenant_update
from services.gcs_service import (
    MAX_FILE_SIZE as MARKET_PRODUCT_IMAGE_MAX_BYTES,
    UploadFileTooLargeError,
    upload_to_gcs,
    validate_upload_size,
)


market_bp = Blueprint("market", __name__, url_prefix="/api/market")
market_admin_bp = Blueprint("market_admin", __name__, url_prefix="/api/admin/market")
MARKET_PRODUCT_IMAGE_MAX_FILES = 5


def _resolve_tenant(slug: str) -> TenantProfile:
    slug_clean = (slug or "").strip().lower()
    if not slug_clean:
        abort(make_response(jsonify({"error": "Slug requerido"}), 400))

    try:
        # Reutilizamos el mismo resolver multitenant usado en el resto de la app
        # para respetar aliases y configuraciones de dominio.
        # Esto incluye la creación "lazy" de demos si no existen en BD.
        tenant = resolve_tenant_only(tenant_slug=slug_clean, require_explicit_slug=True)
    except TenantResolutionError:
        tenant = None

    if tenant is None:
        tenant = (
            TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug_clean)
            .order_by(TenantProfile.id.desc())
            .first()
        )

    if tenant is None:
        abort(make_response(jsonify({"error": "Tenant no encontrado"}), 404))

    g.tenant_profile = tenant
    g.tenant_profile_slug = tenant.slug
    return tenant


def _tenant_owner(tenant: TenantProfile) -> Optional[User]:
    return tenant.municipio or tenant.pyme


_CATALOG_IMAGE_KEYS = (
    "imagen_url",
    "image_url",
    "foto",
    "foto_url",
    "photo",
    "photo_url",
    "thumbnail",
    "thumbnail_url",
)

_CATALOG_GALLERY_KEYS = (
    "gallery_urls",
    "imagenes",
    "images",
    "image_urls",
    "fotos",
    "photos",
)


def _split_image_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, dict):
        raw_values = list(value.values())
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                raw_values = parsed if isinstance(parsed, list) else [text]
            except Exception:
                raw_values = [text]
        else:
            raw_values = re.split(r"[\n;,|]+", text)

    urls: list[str] = []
    for raw in raw_values:
        candidate = str(raw or "").strip()
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls[:12]


def _image_payload_from_product_payload(payload: dict) -> tuple[str | None, list[str]]:
    primary = None
    for key in _CATALOG_IMAGE_KEYS:
        value = payload.get(key)
        if value:
            primary = str(value).strip()
            break

    gallery: list[str] = []
    for key in _CATALOG_GALLERY_KEYS:
        gallery.extend(_split_image_values(payload.get(key)))
    if primary and primary not in gallery:
        gallery.insert(0, primary)
    if not primary and gallery:
        primary = gallery[0]
    return primary, list(dict.fromkeys(gallery))[:12]


def _merge_product_image_metadata(producto: CatalogoItem, payload: dict) -> None:
    primary, gallery = _image_payload_from_product_payload(payload)
    if primary is not None:
        producto.imagen_url = primary

    existing = producto.extra_metadata if isinstance(producto.extra_metadata, dict) else {}
    metadata = dict(existing)
    if gallery:
        metadata["gallery_urls"] = gallery
        metadata["image_count"] = len(gallery)
        metadata["image_status"] = "ready"
    elif "imagen_url" in payload or "image_url" in payload or "gallery_urls" in payload or "imagenes" in payload:
        metadata.setdefault("gallery_urls", [])
        metadata["image_status"] = "missing"
    if payload.get("image_alt") or payload.get("alt"):
        metadata["image_alt"] = payload.get("image_alt") or payload.get("alt")
    if payload.get("image_source"):
        metadata["image_source"] = payload.get("image_source")
    producto.extra_metadata = metadata


def _resolve_session_identifier() -> str:
    """Resolve a stable identifier for the cart owner.

    Priority:
      1) Omnichannel conversation id (if present)
      2) Anonymous identifiers from headers/cookies
      3) Contact key (if already resolved upstream)
      4) Flask session fallback
    """
    contact_identity = getattr(g, "contact_identity", {}) if hasattr(g, "contact_identity") else {}
    conversation_id = str((contact_identity or {}).get("conversation_id") or "").strip()
    if conversation_id:
        return conversation_id

    # 1. Try Anon-Id from headers (most reliable for PWA/Widgets)
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.headers.get("x-anon-id")
        or request.headers.get("anon-id")
    )
    if anon_id:
        return anon_id

    # 2. Try cookie (if standard web client)
    anon_id_cookie = request.cookies.get("anon_id") or request.cookies.get("chatboc_anon_id")
    if anon_id_cookie:
        return anon_id_cookie

    # 3. If available, reuse canonical contact key resolved by app middleware.
    contact_key = str((contact_identity or {}).get("contact_key") or "").strip()
    if contact_key:
        return contact_key

    # 4. Fallback to flask session (volatile if cookies blocked)
    session_id = session.get("market_session_id")
    if not session_id:
        import secrets

        session_id = secrets.token_hex(16)
        session["market_session_id"] = session_id
        session.modified = True
    return session_id


def _request_channel() -> str:
    return normalize_sales_channel(
        request.headers.get("X-Sales-Channel")
        or request.headers.get("X-Channel")
        or request.args.get("channel")
    )


def _runtime_rewards_profile(tenant_id: int, puntos_en_carrito: float) -> Dict[str, object]:
    """Return rewards payload without forcing demo fixtures in production."""

    demo_mode_enabled = bool(current_app.config.get("ENABLE_DEMO_MODE", False))
    if demo_mode_enabled:
        payload = reward_profile_for_tenant(tenant_id, puntos_en_carrito)
        payload["mode"] = "demo"
        return payload

    puntos = round(max(float(puntos_en_carrito or 0.0), 0.0), 2)
    return {
        "mode": "disabled",
        "balance_resumen": {
            "saldo_disponible": 0.0,
            "puntos_en_carrito": puntos,
            "saldo_estimado_post_compra": 0.0,
        },
    }


def _resolve_contact_key(user: User, session_id: str) -> Optional[str]:
    identity = getattr(g, "contact_identity", None)
    if isinstance(identity, dict):
        contact_key = str(identity.get("contact_key") or "").strip()
        if contact_key:
            return contact_key

    contact = resolve_order_contact_payload(
        user=user,
        session_id=session_id,
        channel=_request_channel(),
    )
    return contact.get("contact_key")


def _get_or_create_cart_for_user(
    tenant: TenantProfile,
    user: User,
    *,
    create_if_missing: bool = True,
) -> Optional[MarketCart]:
    """Return the open cart for the given tenant/user combination.

    The cart is anchored to ``user.id`` when available and falls back to a
    session identifier to preserve continuity for partially authenticated
    flows.  If ``create_if_missing`` is False, None is returned instead of
    creating a new record.
    """

    session_id = _resolve_session_identifier()
    user_id = getattr(user, "id", None)

    base_query = MarketCart.legacy_safe_query().filter(
        MarketCart.tenant_id == tenant.id,
        MarketCart.status == "open",
    )

    stored_carts = session.get("market_cart_ids") or {}
    cart_id = stored_carts.get(tenant.slug)
    cart = None

    if cart_id:
        cart = base_query.filter(MarketCart.id == cart_id).first()
        if cart and not (
            cart.session_id == session_id
            or (user_id and cart.user_id == user_id)
        ):
            cart = None

    if cart is None and user_id:
        cart = (
            base_query.filter(MarketCart.user_id == user_id)
            .order_by(MarketCart.updated_at.desc())
            .first()
        )

    if cart is None:
        cart = (
            base_query.filter(MarketCart.session_id == session_id)
            .order_by(MarketCart.updated_at.desc())
            .first()
        )

    if cart is None:
        if not create_if_missing:
            return None

        cart = MarketCart(
            tenant_id=tenant.id,
            user_id=user_id,
            session_id=session_id,
            contact_phone=getattr(user, "telefono", None),
            contact_name=getattr(user, "name", None),
            contact_email=getattr(user, "email", None),
            contact_key=_resolve_contact_key(user=user, session_id=session_id),
            channel=_request_channel(),
        )
        db.session.add(cart)
        db.session.commit()
    else:
        modified = False
        if cart.session_id != session_id:
            cart.session_id = session_id
            modified = True
        if user_id and cart.user_id is None:
            cart.user_id = user_id
            if not cart.contact_phone:
                cart.contact_phone = getattr(user, "telefono", None)
            if not cart.contact_name:
                cart.contact_name = getattr(user, "name", None)
            modified = True
        resolved_contact_key = _resolve_contact_key(user=user, session_id=session_id)
        if resolved_contact_key and cart.contact_key != resolved_contact_key:
            cart.contact_key = resolved_contact_key
            modified = True
        resolved_contact = resolve_order_contact_payload(user=user, session_id=session_id, channel=_request_channel())
        if resolved_contact.get("email") and not cart.contact_email:
            cart.contact_email = resolved_contact.get("email")
            modified = True
        if resolved_contact.get("channel") and cart.channel != resolved_contact.get("channel"):
            cart.channel = resolved_contact.get("channel")
            modified = True
        if modified:
            db.session.commit()

    if stored_carts.get(tenant.slug) != cart.id:
        stored_carts[tenant.slug] = cart.id
        session["market_cart_ids"] = stored_carts
        session.modified = True

    return cart


def _get_or_create_cart(tenant: TenantProfile) -> MarketCart:
    return _get_or_create_cart_for_user(tenant, current_user)


def _product_query_for_tenant(owner: User, tenant: TenantProfile, *, include_unavailable: bool = False):
    """Return productos estrictamente asociados al tenant."""

    filters = [CatalogoItem.tenant_id == tenant.id, CatalogoItem.user_id == owner.id]

    if not include_unavailable:
        # Compatibilidad: algunos registros antiguos pueden tener ``disponible``
        # en NULL, por lo que se tratan como disponibles a menos que se marque
        # explícitamente como False.
        filters.append(or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None)))

    return (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter(*filters)
        .order_by(func.lower(CatalogoItem.nombre))
    )


def _pricing_snapshot(product: CatalogoItem) -> Dict[str, object]:
    formatted = _formatear_producto(
        {
            "nombre": product.nombre,
            "categoria": product.categoria,
            "descripcion": product.descripcion,
            "sku": product.sku,
            "unidad": product.unidad,
            "precio_str": product.precio,
            "cantidad": product.cantidad,
            "marca": product.marca,
            "imagen_url": product.imagen_url,
            "descripcion_corta": product.descripcion_corta,
            "promocion_info": product.promocion_info,
            "precio_por_caja": product.precio_por_caja,
            "unidad_por_caja": product.unidad_por_caja,
            "moneda": product.moneda,
            "precio_float": product.precio_monetario,
            "extra_metadata": product.extra_metadata,
        }
    )

    precio_unitario = formatted.get("precio_unitario")
    moneda = formatted.get("moneda") or "ARS"
    modalidad = formatted.get("modalidad") or product.modalidad
    precio_monetario = None
    precio_puntos = None

    if isinstance(precio_unitario, (int, float)):
        if moneda == "PTS":
            precio_puntos = int(precio_unitario)
        else:
            precio_monetario = float(precio_unitario)
    else:
        _, precio_float, _ = parse_precio_flexible(str(precio_unitario))
        if precio_float is not None:
            if moneda == "PTS":
                precio_puntos = int(precio_float)
            else:
                precio_monetario = float(precio_float)

    return {
        "price_text": product.precio,
        "price_monetary": precio_monetario,
        "price_points": precio_puntos,
        "currency": moneda,
        "modalidad": modalidad,
        "formatted": formatted,
    }


def _resolve_wallet_balance(tenant: TenantProfile) -> Optional[float]:
    """Return a virtual wallet balance if configured for the tenant.

    Some tenants may preload a demo balance for pesos/dinero; if absent we
    return ``None`` so the UI can gracefully omit the indicator.
    """

    cfg = getattr(tenant, "configuracion", None) or {}
    balance = cfg.get("wallet_balance")
    if balance is None:
        balance = cfg.get("saldo_virtual")
    try:
        return float(balance) if balance is not None else None
    except (TypeError, ValueError):
        return None


def _cart_recommendations(owner: User, tenant: TenantProfile, *, exclude_ids: list[int], limit: int = 3) -> list[dict]:
    query = _product_query_for_tenant(owner, tenant, include_unavailable=False)
    if exclude_ids:
        query = query.filter(~CatalogoItem.id.in_(exclude_ids))
    rows = query.order_by(
        CatalogoItem.promocion_info.isnot(None).desc(),
        CatalogoItem.timestamp.desc().nullslast(),
        func.lower(CatalogoItem.nombre),
    ).limit(limit * 2).all()

    recommendations: list[dict] = []
    for item in rows:
        formatted = _formatear_producto(
            {
                "nombre": item.nombre,
                "descripcion": item.descripcion,
                "categoria": item.categoria,
                "precio_str": item.precio,
                "moneda": item.moneda,
                "modalidad": item.modalidad,
                "imagen_url": item.imagen_url,
                "promocion_info": item.promocion_info,
            }
        )
        recommendations.append(
            {
                "catalogo_item_id": item.id,
                "title": item.nombre,
                "category": item.categoria,
                "price_label": formatted.get("precio_texto") or item.precio,
                "promotion": item.promocion_info,
                "image_url": item.imagen_url,
                "cta": {"action": "add_to_cart", "product_id": item.id},
            }
        )
        if len(recommendations) >= limit:
            break
    return recommendations


def _cart_summary(cart: MarketCart, owner: User, *, event: Optional[str] = None) -> Dict[str, object]:
    items = list(cart.items.all())
    product_ids = [item.product_id for item in items if item.product_id]
    products: Dict[int, CatalogoItem] = {}
    if product_ids:
        rows = (
            _product_query_for_tenant(owner, cart.tenant, include_unavailable=True)
            .filter(CatalogoItem.id.in_(product_ids))
            .all()
        )
        products = {row.id: row for row in rows}

    enriched: List[Dict[str, object]] = []
    totals_by_currency: Dict[str, float] = {}
    total_points = 0
    total_count = 0

    for entry in items:
        product = products.get(entry.product_id)
        formatted = _pricing_snapshot(product) if product else None
        price_text = entry.price_text
        price_monetary = entry.price_monetary
        price_points = entry.price_points
        currency = entry.currency or (formatted["currency"] if formatted else "ARS")
        modalidad = entry.modalidad or (formatted["modalidad"] if formatted else "venta")

        if formatted:
            price_text = formatted["formatted"].get("precio_texto") or formatted["formatted"].get("precio_unitario")
            if price_monetary is None:
                price_monetary = formatted["price_monetary"]
            if price_points is None:
                price_points = formatted["price_points"]

        subtotal = None
        subtotal_points = None
        if currency == "PTS" or modalidad == "canje":
            if price_points is not None:
                subtotal_points = price_points * entry.quantity
                total_points += subtotal_points
        elif price_monetary is not None:
            subtotal = float(price_monetary) * entry.quantity
            totals_by_currency[currency] = totals_by_currency.get(currency, 0.0) + subtotal

        total_count += entry.quantity
        enriched.append(
            {
                "catalogo_item_id": entry.product_id,
                "nombre": product.nombre if product else entry.name_snapshot,
                "cantidad": entry.quantity,
                "precio_unitario": price_monetary,
                "precio_unitario_texto": price_text,
                "subtotal": subtotal,
                "subtotal_puntos": subtotal_points,
                "moneda": currency,
                "modalidad": modalidad,
                "imagen_url": formatted["formatted"].get("imagen_url") if formatted else None,
                "descripcion": formatted["formatted"].get("descripcion") if formatted else None,
            }
        )

    total_default_currency = totals_by_currency.get("ARS", 0.0)
    wallet_balance = _resolve_wallet_balance(cart.tenant)
    balances = {"wallet_balance": wallet_balance} if wallet_balance is not None else {}
    promotion_summary = None
    promo_input = [
        {
            "catalogo_item_id": item.get("catalogo_item_id"),
            "cantidad": item.get("cantidad"),
            "nombre_producto": item.get("nombre"),
            "precio_unitario_original": item.get("precio_unitario"),
            "moneda": item.get("moneda"),
        }
        for item in enriched
        if item.get("catalogo_item_id")
    ]
    if promo_input and getattr(owner, "id", None):
        try:
            promotion_summary = promocion_service.aplicar_promociones_al_carrito(
                owner.id,
                promo_input,
                cliente_user_id=cart.user_id,
            )
        except Exception:
            promotion_summary = None

    customer_profile = build_customer_profile(
        user=cart.user,
        payload={
            "contacto": {
                "nombre": cart.contact_name,
                "telefono": cart.contact_phone,
                "email": cart.contact_email,
            },
            "anon_id": cart.session_id if str(cart.session_id or "").startswith(("anon:", "wa:", "whatsapp", "+")) else None,
        },
        session_id=cart.session_id,
        channel=cart.channel,
    )
    recommendations = _cart_recommendations(
        owner,
        cart.tenant,
        exclude_ids=[item.product_id for item in items if item.product_id],
    )
    support_phone = getattr(owner, "telefono", None)
    commercial_stage = "cart_active" if total_count else "cart_empty"
    identity = getattr(g, "contact_identity", {}) if hasattr(g, "contact_identity") else {}
    conversation_id = str((identity or {}).get("conversation_id") or "").strip() or None
    portal_base_path = f"/{cart.tenant.slug}/portal" if getattr(cart.tenant, "slug", None) else None

    resumen = {
        "tenant_id": cart.tenant_id,
        "cart_id": cart.id,
        "contact_key": cart.contact_key,
        "channel": cart.channel or "web",
        "items": enriched,
        "items_count": total_count,
        "totales_monedas": {k: round(v, 2) for k, v in totals_by_currency.items()},
        "total_estimado": round(total_default_currency, 2),
        "total_puntos_estimado": total_points,
        "moneda": "ARS",
        "badge_count": total_count,
        "balances": balances,
        "contacto": {
            "nombre": cart.contact_name,
            "telefono": cart.contact_phone,
            "email": cart.contact_email,
        },
        "customer_profile": customer_profile,
        "commercial_state": {
            "stage": commercial_stage,
            "channel": cart.channel or "web",
            "has_contact": bool(cart.contact_name or cart.contact_phone or cart.contact_email),
            "supports_handoff": (cart.channel or "web") in {"whatsapp", "phone", "manual_admin"},
        },
        "continuity": {
            "resume_key": cart.contact_key or cart.session_id,
            "portal_path": portal_base_path,
            "portal_links": {
                "home": portal_base_path,
                "orders": f"{portal_base_path}/pedidos" if portal_base_path else None,
                "profile": f"{portal_base_path}/perfil" if portal_base_path else None,
            },
            "conversation_id": conversation_id,
            "preferred_handoff_channel": "whatsapp" if support_phone else (cart.channel or "web"),
        },
        "suggested_actions": [
            {"id": "checkout", "label": "Finalizar compra", "variant": "primary", "enabled": total_count > 0},
            {"id": "view_rewards", "label": "Ver puntos", "variant": "secondary", "enabled": True},
            {"id": "handoff_whatsapp", "label": "Seguir por WhatsApp", "variant": "ghost", "enabled": bool(support_phone)},
        ],
        "recommendations": recommendations,
        "ui_signals": {
            "event": event or "refresh",
            "animation": "cart-burst" if event else "soft-pulse",
            "badge": total_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }
    if promotion_summary:
        resumen["promotions"] = {
            "items_detalle": promotion_summary.get("items_detalle", []),
            "total_ahorrado": promotion_summary.get("total_ahorrado_final", 0),
            "total_con_descuento": promotion_summary.get("total_final_con_descuento", resumen["total_estimado"]),
            "promociones_aplicadas": promotion_summary.get("promociones_aplicadas_nombres", []),
            "promo_total_carrito": promotion_summary.get("promo_total_carrito_aplicada_info"),
        }

    resumen["recompensas_demo"] = _runtime_rewards_profile(cart.tenant_id, total_points)
    resumen["wallet"] = (resumen.get("recompensas_demo") or {}).get("balance_resumen")
    resumen["checkout_preview"] = {
        "state": "ready" if total_count > 0 else "empty",
        "supports_points": total_points > 0,
        "next_step_label": "Continuar al checkout" if total_count > 0 else "Agregá productos para avanzar",
    }
    return resumen


def _empty_cart_summary(tenant: TenantProfile) -> Dict[str, object]:
    rewards = _runtime_rewards_profile(tenant.id, 0.0)
    portal_base_path = f"/{tenant.slug}/portal" if getattr(tenant, "slug", None) else None
    return {
        "tenant_id": tenant.id,
        "cart_id": None,
        "items": [],
        "items_count": 0,
        "totales_monedas": {},
        "total_estimado": 0,
        "total_puntos_estimado": 0,
        "moneda": "ARS",
        "badge_count": 0,
        "contacto": {},
        "recompensas_demo": rewards,
        "wallet": rewards.get("balance_resumen"),
        "continuity": {
            "resume_key": None,
            "portal_path": portal_base_path,
            "portal_links": {
                "home": portal_base_path,
                "orders": f"{portal_base_path}/pedidos" if portal_base_path else None,
                "profile": f"{portal_base_path}/perfil" if portal_base_path else None,
            },
            "conversation_id": None,
            "preferred_handoff_channel": "web",
        },
        "suggested_actions": [
            {"id": "browse_catalog", "label": "Explorar catálogo", "variant": "primary", "enabled": True},
            {"id": "view_rewards", "label": "Ver puntos", "variant": "secondary", "enabled": True},
        ],
        "recommendations": [],
        "checkout_preview": {
            "state": "empty",
            "supports_points": False,
            "next_step_label": "Agregá productos para avanzar",
        },
        "ui_signals": {"animation": "idle", "badge": "rest"},
    }


def _decimal_or_none(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _checkout_money_totals(summary: Dict[str, object]) -> Dict[str, object]:
    original = _decimal_or_none(summary.get("total_estimado"))
    if original is None:
        return {
            "total_monetary": None,
            "total_original": None,
            "total_discounted": None,
            "discount_total": 0.0,
            "discount_applied": False,
        }

    promotions = summary.get("promotions") if isinstance(summary.get("promotions"), dict) else {}
    discounted = _decimal_or_none((promotions or {}).get("total_con_descuento"))
    if discounted is not None and Decimal("0") <= discounted <= original:
        discount_total = original - discounted
        return {
            "total_monetary": float(discounted),
            "total_original": float(original),
            "total_discounted": float(discounted),
            "discount_total": float(discount_total),
            "discount_applied": discount_total > 0,
        }

    return {
        "total_monetary": float(original),
        "total_original": float(original),
        "total_discounted": float(original),
        "discount_total": 0.0,
        "discount_applied": False,
    }


def _mercadopago_preference_items_for_checkout(
    cart_items: list[MarketCartItem],
    *,
    tenant: TenantProfile,
    total_monetary,
    discount_applied: bool,
) -> list[dict]:
    if discount_applied:
        total = _decimal_or_none(total_monetary)
        if total is None or total <= 0:
            return []
        return [
            {
                "title": f"Pedido {tenant.nombre or tenant.slug or 'Chatboc'}",
                "quantity": 1,
                "unit_price": float(total),
                "currency_id": "ARS",
            }
        ]

    preference_items = []
    for entry in cart_items:
        if entry.price_monetary:
            preference_items.append(
                {
                    "title": entry.name_snapshot or "Producto",
                    "quantity": entry.quantity,
                    "unit_price": float(entry.price_monetary),
                    "currency_id": entry.currency or "ARS",
                }
            )
    return preference_items


@market_bp.get("/<slug>/catalog")
@cutover_writer_view
def public_catalog(slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    ensure_seed_catalog(owner, tenant)

    contract_mode = str(request.args.get("contract") or request.args.get("view") or "").strip().lower()
    if contract_mode in {"marketplace", "market", "v2", "full"}:
        return jsonify(
            build_public_market_catalog_contract(
                tenant,
                owner,
                base_web_url=current_app.config.get("APP_BASE_URL", "https://chatboc.ar"),
                categoria=request.args.get("categoria"),
                q=request.args.get("q"),
                precio_min=request.args.get("precio_min"),
                precio_max=request.args.get("precio_max"),
                en_promocion=request.args.get("en_promocion"),
                sort=request.args.get("sort"),
            )
        )

    categoria = request.args.get("categoria")
    search_text = request.args.get("q")

    query = _product_query_for_tenant(owner, tenant)
    if categoria:
        categoria_norm = categoria.strip().lower()
        if categoria_norm:
            query = query.filter(func.lower(CatalogoItem.categoria) == categoria_norm)

    items = query.all()

    productos: List[Dict[str, object]] = []
    for item in items:
        prod = _formatear_producto(
            {
                "nombre": item.nombre,
                "categoria": item.categoria,
                "descripcion": item.descripcion,
                "sku": item.sku,
                "unidad": item.unidad,
                "precio_str": item.precio,
                "cantidad": item.cantidad,
                "marca": item.marca,
                "imagen_url": item.imagen_url,
                "descripcion_corta": item.descripcion_corta,
                "promocion_info": item.promocion_info,
            }
        )
        prod["catalogo_item_id"] = item.id
        prod["tenant_id"] = tenant.id
        productos.append(prod)

    if search_text:
        term = search_text.strip().lower()
        if term:
            filtrados = []
            for prod in productos:
                texto_busqueda = " ".join(
                    str(value or "")
                    for value in (
                        prod.get("nombre"),
                        prod.get("descripcion"),
                        prod.get("categoria"),
                        prod.get("promocion_info"),
                    )
                ).lower()
                if term in texto_busqueda:
                    filtrados.append(prod)
            productos = filtrados

    return jsonify(productos)


@market_bp.get("/<slug>/catalog/<int:product_id>")
@cutover_writer_view
def public_product_detail(slug: str, product_id: int):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    ensure_seed_catalog(owner, tenant)
    producto = (
        _product_query_for_tenant(owner, tenant)
        .filter(CatalogoItem.id == product_id)
        .first()
    )
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    detalle = _formatear_producto(
        {
            "nombre": producto.nombre,
            "categoria": producto.categoria,
            "descripcion": producto.descripcion,
            "sku": producto.sku,
            "unidad": producto.unidad,
            "precio_str": producto.precio,
            "cantidad": producto.cantidad,
            "marca": producto.marca,
            "imagen_url": producto.imagen_url,
            "descripcion_corta": producto.descripcion_corta,
            "promocion_info": producto.promocion_info,
            "pdf_url": getattr(producto, "pdf_url", None),
        }
    )
    detalle["catalogo_item_id"] = producto.id
    detalle["tenant_id"] = tenant.id
    detalle["disponible"] = getattr(producto, "disponible", True)
    return jsonify(detalle)


@market_bp.get("/<slug>/cart")
@market_bp.get("/<slug>/.cart")
@cutover_writer_view
@token_requerido
def public_cart_summary(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    ensure_seed_catalog(owner, tenant)
    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify(_empty_cart_summary(tenant))

    summary = _cart_summary(cart, owner)
    summary["ui_signals"] = {"animation": "cart-burst", "badge": "pulse"}
    return jsonify(summary)


@market_bp.post("/<slug>/cart/add")
@token_requerido
def public_cart_add(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    ensure_seed_catalog(owner, tenant)
    payload = request.get_json(silent=True) or {}
    item_id = payload.get("product_id") or payload.get("catalogo_item_id") or payload.get("item_id")
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return jsonify({"error": "catalogo_item_id requerido"}), 400

    producto = (
        _product_query_for_tenant(owner, tenant)
        .filter(CatalogoItem.id == item_id_int)
        .first()
    )
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    cantidad = payload.get("cantidad") or payload.get("quantity") or 1
    try:
        cantidad_int = max(int(cantidad), 1)
    except (TypeError, ValueError):
        cantidad_int = 1

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=True)
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400
    pricing = _pricing_snapshot(producto)

    cart_item = cart.items.filter(MarketCartItem.product_id == producto.id).first()
    if cart_item:
        cart_item.quantity += cantidad_int
    else:
        cart_item = MarketCartItem(
            cart_id=cart.id,
            product_id=producto.id,
            quantity=cantidad_int,
            price_text=pricing.get("price_text"),
            price_monetary=pricing.get("price_monetary"),
            price_points=pricing.get("price_points"),
            currency=pricing.get("currency"),
            modalidad=pricing.get("modalidad"),
            name_snapshot=producto.nombre,
        )
        db.session.add(cart_item)

    db.session.commit()
    return jsonify(_cart_summary(cart, owner, event="add"))


@market_bp.post("/<slug>/cart/remove")
@token_requerido
def public_cart_remove(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    payload = request.get_json(silent=True) or {}
    item_id = payload.get("product_id") or payload.get("catalogo_item_id") or payload.get("item_id")
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return jsonify({"error": "catalogo_item_id requerido"}), 400

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify({"error": "Carrito no encontrado"}), 404
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id_int).first()
    if cart_item is None:
        return jsonify({"error": "Item no encontrado en el carrito"}), 404

    db.session.delete(cart_item)
    db.session.commit()
    return jsonify(_cart_summary(cart, owner, event="remove"))


@market_bp.post("/<slug>/cart/update")
@token_requerido
def public_cart_update(current_user, slug: str):
    """Update item quantity within the tenant cart.

    Accepts ``product_id`` (or ``catalogo_item_id``/``item_id``) and ``quantity``.
    Quantity ``0`` removes the item. Positive quantities replace the stored amount.
    """

    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    payload = request.get_json(silent=True) or {}
    item_id = payload.get("product_id") or payload.get("catalogo_item_id") or payload.get("item_id")
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return jsonify({"error": "catalogo_item_id requerido"}), 400

    cantidad = payload.get("cantidad") or payload.get("quantity")
    try:
        cantidad_int = int(cantidad)
    except (TypeError, ValueError):
        return jsonify({"error": "Cantidad inválida"}), 400

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify({"error": "Carrito no encontrado"}), 404
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id_int).first()

    if cart_item is None:
        return jsonify({"error": "Item no encontrado en el carrito"}), 404

    if cantidad_int <= 0:
        db.session.delete(cart_item)
    else:
        cart_item.quantity = cantidad_int

    db.session.commit()
    return jsonify(_cart_summary(cart, owner, event="update"))


@market_bp.post("/<slug>/cart/clear")
@token_requerido
def public_cart_clear_authenticated(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify(_empty_cart_summary(tenant))
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400

    for entry in list(cart.items.all()):
        db.session.delete(entry)

    cart.estado = "abierto"
    db.session.commit()
    return jsonify(_cart_summary(cart, owner, event="clear"))


@market_bp.post("/<slug>/cart/checkout")
@token_requerido
def public_cart_checkout(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify({"error": "Carrito no encontrado"}), 404
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400
    if cart.items.count() == 0:
        return jsonify({"error": "El carrito está vacío"}), 400

    cart.estado = "confirmado"
    db.session.commit()

    summary = _cart_summary(cart, owner)
    summary["estado"] = cart.estado
    summary["status"] = cart.status
    return jsonify(summary)


@market_bp.post("/<slug>/checkout/start")
@token_requerido
def start_checkout(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify({"error": "Carrito no encontrado"}), 404
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400
    if cart.items.count() == 0:
        return jsonify({"error": "El carrito está vacío"}), 400

    payload = request.get_json(silent=True) or {}
    contact = resolve_order_contact_payload(
        user=current_user,
        payload=payload,
        session_id=cart.session_id,
        channel=_request_channel(),
    )
    telefono = contact.get("phone")
    nombre = contact.get("name")
    email = contact.get("email")

    if not telefono:
        return (
            jsonify({"error": "Teléfono requerido para iniciar el checkout"}),
            400,
        )

    cart.contact_phone = telefono
    cart.contact_name = nombre
    cart.contact_email = email
    cart.contact_key = contact.get("contact_key")
    cart.channel = contact.get("channel")
    if current_user.is_authenticated and cart.user_id is None:
        cart.user_id = current_user.id

    summary = _cart_summary(cart, owner, event="checkout")

    balances = summary.get("balances") or {}
    if balances.get("insufficient_points"):
        return (
            jsonify({
                "error": "Puntos insuficientes para confirmar el pedido",
                "detalle": {
                    "puntos_disponibles": balances.get("points_available"),
                    "puntos_requeridos": summary.get("total_puntos_estimado"),
                },
            }),
            400,
        )

    checkout_totals = _checkout_money_totals(summary)
    total_monetary = checkout_totals["total_monetary"]
    total_points = summary.get("total_puntos_estimado")
    order = MarketOrder(
        tenant_id=tenant.id,
        user_id=cart.user_id,
        cart_id=cart.id,
        status="pending",
        contact_name=nombre,
        contact_phone=telefono,
        contact_email=email,
        contact_key=contact.get("contact_key"),
        channel=contact.get("channel"),
        session_id=cart.session_id,
        total_monetary=total_monetary,
        total_points=total_points,
        currency="ARS",
        metadata_payload={
            "totales_monedas": summary.get("totales_monedas", {}),
            "promotions": summary.get("promotions"),
            "pricing": {
                "total_original": checkout_totals["total_original"],
                "total_discounted": checkout_totals["total_discounted"],
                "discount_total": checkout_totals["discount_total"],
                "discount_applied": checkout_totals["discount_applied"],
            },
        },
    )
    db.session.add(order)

    cart_items = list(cart.items.all())
    for entry in cart_items:
        db.session.add(
            MarketOrderItem(
                order=order,
                product_id=entry.product_id,
                quantity=entry.quantity,
                price_monetary=entry.price_monetary,
                price_points=entry.price_points,
                currency=entry.currency,
                modalidad=entry.modalidad,
                name_snapshot=entry.name_snapshot,
                extra={"price_text": entry.price_text},
            )
        )

    # Mercado Pago Integration
    mp_init_point = None
    mp_preference_id = None

    tenant_cfg = tenant.configuracion or {}
    mp_token = tenant_cfg.get("mercadopago_access_token") or os.getenv("MERCADOPAGO_ACCESS_TOKEN")

    if mp_token and total_monetary and total_monetary > 0:
        try:
            # Check stock
            for entry in cart_items:
                product = CatalogoItem.query.get(entry.product_id)
                if product:
                    # Try to parse stock if numeric
                    try:
                        stock_val = float(product.cantidad) if product.cantidad else 0
                        if stock_val < entry.quantity:
                            return jsonify({"error": f"Stock insuficiente para {product.nombre}", "stock_disponible": stock_val}), 400
                    except (ValueError, TypeError):
                        pass  # Ignore if stock is text like "Consultar"

            preference_items = _mercadopago_preference_items_for_checkout(
                cart_items,
                tenant=tenant,
                total_monetary=total_monetary,
                discount_applied=bool(checkout_totals["discount_applied"]),
            )

            if preference_items:
                pref_payload = {
                    "items": preference_items,
                    "external_reference": f"MO-{order.id}",
                    "back_urls": {
                        "success": f"{os.getenv('APP_BASE_URL', 'https://chatboc.ar')}/{tenant.slug}/checkout/success",
                        "failure": f"{os.getenv('APP_BASE_URL', 'https://chatboc.ar')}/{tenant.slug}/checkout/failure",
                        "pending": f"{os.getenv('APP_BASE_URL', 'https://chatboc.ar')}/{tenant.slug}/checkout/pending"
                    },
                    "auto_return": "approved",
                }

                resp = requests.post(
                    "https://api.mercadopago.com/checkout/preferences",
                    json=pref_payload,
                    headers={"Authorization": f"Bearer {mp_token}"},
                    timeout=10
                )

                if resp.status_code in (200, 201):
                    mp_data = resp.json()
                    mp_init_point = mp_data.get("init_point")
                    mp_preference_id = mp_data.get("id")

                    # Update metadata
                    meta = order.metadata_payload or {}
                    meta["mp_preference_id"] = mp_preference_id
                    meta["mp_init_point"] = mp_init_point
                    meta["mp_items"] = preference_items
                    order.metadata_payload = meta
                else:
                    print(f"MP Error: {resp.text}")

        except Exception as e:
            print(f"MP Exception: {e}")

    cart.status = "submitted"
    db.session.commit()

    return jsonify(
        {
            "order_id": order.id,
            "status": order.status,
            "tenant_id": tenant.id,
            "cart_id": cart.id,
            "contacto": {"nombre": nombre, "telefono": telefono},
            "total_monetary": float(total_monetary or 0.0) if total_monetary is not None else None,
            "total_original": checkout_totals["total_original"],
            "total_discounted": checkout_totals["total_discounted"],
            "discount_total": checkout_totals["discount_total"],
            "discount_applied": checkout_totals["discount_applied"],
            "promotions": summary.get("promotions"),
            "total_points": total_points,
            "checkout_options": {
                "mercadopago_ready": bool(mp_init_point),
                "preference_id": mp_preference_id,
                "init_point": mp_init_point,
                "gateway_hint": "Mercado Pago",
                "points_enabled": bool(total_points),
            },
        }
    )


@market_bp.get("/<slug>/cart/url")
def public_cart_url(slug: str):
    tenant = _resolve_tenant(slug)
    full_url, base_url, path = _build_public_cart_url(tenant)
    if path == "override":
        path = f"market/{tenant.slug}/.cart"

    return jsonify({"cart_url": full_url, "base_url": base_url, "tenant_slug": tenant.slug, "path": path})


def _resolve_admin_tenant(user, payload):
    """Resolve market admin scope without trusting a caller-selected tenant."""

    payload = payload if isinstance(payload, dict) else {}
    role = canonical_role(getattr(user, "rol", None))
    requested_tenant_id = payload.get("tenant_id")

    if role == ROLE_SUPERADMIN:
        if not is_authorized_superadmin_user(user):
            abort(make_response(jsonify({"error": "Permisos insuficientes"}), 403))
        if requested_tenant_id in (None, ""):
            abort(make_response(jsonify({"error": "tenant_id requerido"}), 400))
        try:
            tenant = db.session.get(TenantProfile, int(requested_tenant_id))
        except (TypeError, ValueError):
            tenant = None
        if tenant is None or getattr(tenant, "is_active", True) is False:
            abort(make_response(jsonify({"error": "Tenant no encontrado"}), 404))
        return tenant

    if role != ROLE_TENANT_ADMIN:
        abort(make_response(jsonify({"error": "Permisos insuficientes"}), 403))

    tenant = auth_tenant_for_user(user)
    if tenant is None or getattr(tenant, "is_active", True) is False:
        abort(make_response(jsonify({"error": "Permisos insuficientes"}), 403))

    if requested_tenant_id not in (None, ""):
        try:
            requested_id = int(requested_tenant_id)
        except (TypeError, ValueError):
            abort(make_response(jsonify({"error": "Permisos insuficientes"}), 403))
        if requested_id != int(tenant.id):
            abort(make_response(jsonify({"error": "Permisos insuficientes"}), 403))
    return tenant

def _serialize_catalog_item(item):
    metadata = item.extra_metadata if isinstance(item.extra_metadata, dict) else {}
    # Standard serialization
    return {
        "id": item.id,
        "nombre": item.nombre,
        "descripcion": item.descripcion,
        "precio": item.precio,
        "precio_monetario": float(item.precio_monetario) if item.precio_monetario else None,
        "moneda": item.moneda,
        "stock": item.cantidad,
        "imagen_url": item.imagen_url,
        "image_url": item.imagen_url,
        "gallery_urls": metadata.get("gallery_urls") or ([item.imagen_url] if item.imagen_url else []),
        "image_status": metadata.get("image_status") or ("ready" if item.imagen_url else "missing"),
        "image_alt": metadata.get("image_alt"),
        "categoria": item.categoria,
        "sku": item.sku,
        "disponible": item.disponible
    }

@market_admin_bp.post("/catalog")
@token_requerido
@require_role("admin", "super_admin")
def admin_create_product(current_user):
    payload = request.get_json(silent=True) or {}
    tenant = _resolve_admin_tenant(current_user, payload)
    owner = _tenant_owner(tenant)
    if owner is None:
        return jsonify({"error": "Tenant sin propietario"}), 404

    nombre = payload.get("nombre") or payload.get("name")
    if not nombre:
        return jsonify({"error": "nombre requerido"}), 400

    precio_decimal = None
    precio_texto = payload.get("precio") or payload.get("price")
    if precio_texto is not None:
        try:
            precio_decimal = Decimal(str(precio_texto))
        except (InvalidOperation, ValueError):
            return jsonify({"error": "precio inválido"}), 400

    producto = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre=nombre,
        descripcion=payload.get("descripcion") or payload.get("description"),
        precio=str(precio_texto) if precio_texto is not None else None,
        precio_monetario=precio_decimal,
        moneda=(payload.get("moneda") or payload.get("currency") or "ARS").upper(),
        categoria=payload.get("categoria"),
        pdf_url=payload.get("pdf_url"),
        disponible=bool(payload.get("disponible", True)),
    )
    _merge_product_image_metadata(producto, payload)
    db.session.add(producto)
    db.session.commit()
    emit_tenant_update(tenant.slug, 'catalog_update', {})
    return jsonify(_serialize_catalog_item(producto)), 201


@market_admin_bp.put("/catalog/<int:product_id>")
@token_requerido
@require_role("admin", "super_admin")
def admin_update_product(current_user, product_id: int):
    payload = request.get_json(silent=True) or {}
    tenant = _resolve_admin_tenant(current_user, payload)

    producto = CatalogoItem.query.filter_by(id=product_id, tenant_id=tenant.id).first()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    if "nombre" in payload or "name" in payload:
        producto.nombre = payload.get("nombre") or payload.get("name") or producto.nombre
    if "descripcion" in payload or "description" in payload:
        producto.descripcion = payload.get("descripcion") or payload.get("description")
    if "categoria" in payload:
        producto.categoria = payload.get("categoria")
    if any(key in payload for key in (*_CATALOG_IMAGE_KEYS, *_CATALOG_GALLERY_KEYS, "image_alt", "alt", "image_source")):
        _merge_product_image_metadata(producto, payload)
    if "pdf_url" in payload:
        producto.pdf_url = payload.get("pdf_url")
    if "moneda" in payload or "currency" in payload:
        producto.moneda = (payload.get("moneda") or payload.get("currency") or producto.moneda or "ARS").upper()
    if "disponible" in payload:
        producto.disponible = bool(payload.get("disponible"))

    if "precio" in payload or "price" in payload:
        precio_texto = payload.get("precio") or payload.get("price")
        if precio_texto is None:
            producto.precio = None
            producto.precio_monetario = None
        else:
            try:
                producto.precio_monetario = Decimal(str(precio_texto))
            except (InvalidOperation, ValueError):
                return jsonify({"error": "precio inválido"}), 400
            producto.precio = str(precio_texto)

    db.session.commit()
    emit_tenant_update(tenant.slug, 'catalog_update', {})
    return jsonify(_serialize_catalog_item(producto))


@market_admin_bp.post("/catalog/<int:product_id>/images")
@token_requerido
@require_role("admin", "super_admin")
def admin_upload_product_image(current_user, product_id: int):
    set_upload_request_limit(
        MARKET_PRODUCT_IMAGE_MAX_BYTES,
        max_files=MARKET_PRODUCT_IMAGE_MAX_FILES,
    )
    payload = {}
    payload.update(request.args.to_dict())
    try:
        payload.update(request.form.to_dict())
        image_files = (
            request.files.getlist("image")
            + request.files.getlist("images")
            + request.files.getlist("file")
        )
    except RequestEntityTooLarge:
        return jsonify({
            "error": "El lote de imágenes supera el límite permitido.",
            "code": "file_too_large",
        }), 413

    image_files = [file for file in image_files if file and file.filename]
    if len(image_files) > MARKET_PRODUCT_IMAGE_MAX_FILES:
        return jsonify({
            "error": "El lote supera la cantidad máxima de imágenes permitida.",
            "code": "too_many_files",
            "max_files": MARKET_PRODUCT_IMAGE_MAX_FILES,
        }), 413

    for file in image_files:
        if not (file.mimetype or "").lower().startswith("image/"):
            return jsonify({"error": "Solo se permiten imágenes para este endpoint"}), 400
        try:
            validate_upload_size(file, max_bytes=MARKET_PRODUCT_IMAGE_MAX_BYTES)
        except UploadFileTooLargeError:
            return jsonify({
                "error": "Una imagen supera el límite permitido.",
                "code": "file_too_large",
                "max_file_bytes": MARKET_PRODUCT_IMAGE_MAX_BYTES,
            }), 413

    tenant = _resolve_admin_tenant(current_user, payload)

    producto = CatalogoItem.query.filter_by(id=product_id, tenant_id=tenant.id).first()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    uploaded = []
    for file in image_files:
        try:
            result = upload_to_gcs(
                file,
                kind="catalog_product_images",
                max_file_size=MARKET_PRODUCT_IMAGE_MAX_BYTES,
            )
        except UploadFileTooLargeError:
            return jsonify({
                "error": "Una imagen supera el límite permitido.",
                "code": "file_too_large",
                "max_file_bytes": MARKET_PRODUCT_IMAGE_MAX_BYTES,
            }), 413
        if not result:
            return jsonify({"error": "No se pudo subir la imagen"}), 500
        uploaded.append(result["public_url"])

    direct_urls = _split_image_values(payload.get("image_url") or payload.get("imagen_url"))
    uploaded.extend(url for url in direct_urls if url not in uploaded)

    if not uploaded:
        return jsonify({"error": "Adjunta una imagen o envia image_url"}), 400

    metadata = producto.extra_metadata if isinstance(producto.extra_metadata, dict) else {}
    gallery = list(metadata.get("gallery_urls") or [])
    replace = str(payload.get("replace") or "").strip().lower() in {"1", "true", "si", "sí"}
    if replace:
        gallery = []
    for url in uploaded:
        if url not in gallery:
            gallery.append(url)

    primary = payload.get("primary_image_url") or (uploaded[0] if str(payload.get("make_primary", "true")).lower() not in {"0", "false", "no"} else producto.imagen_url)
    _merge_product_image_metadata(
        producto,
        {
            "imagen_url": primary,
            "gallery_urls": gallery,
            "image_source": "admin_upload",
            "image_alt": payload.get("image_alt") or payload.get("alt"),
        },
    )
    db.session.commit()
    emit_tenant_update(tenant.slug, 'catalog_update', {"product_id": producto.id, "source": "product_image_upload"})
    return jsonify(
        {
            "ok": True,
            "contract_version": "market.product_images.v1",
            "product": _serialize_catalog_item(producto),
            "uploaded_urls": uploaded,
        }
    )


@market_admin_bp.delete("/catalog/<int:product_id>")
@token_requerido
@require_role("admin", "super_admin")
def admin_delete_product(current_user, product_id: int):
    payload = request.get_json(silent=True) or {}
    tenant = _resolve_admin_tenant(current_user, payload)

    producto = CatalogoItem.query.filter_by(id=product_id, tenant_id=tenant.id).first()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    db.session.delete(producto)
    db.session.commit()
    emit_tenant_update(tenant.slug, 'catalog_update', {})
    return jsonify({"deleted": product_id, "tenant_id": tenant.id})


@market_admin_bp.get("/orders")
@token_requerido
@require_role("admin", "super_admin")
def admin_list_orders(current_user):
    # payload for resolving tenant might come from query string if admin views multiple tenants,
    # but _resolve_admin_tenant uses 'payload' dict usually from json body.
    # Here we simulate payload from args for GET
    payload = request.args.to_dict()
    tenant = _resolve_admin_tenant(current_user, payload)

    query = MarketOrder.legacy_safe_query().filter_by(tenant_id=tenant.id)
    status = payload.get('status')
    if status:
        query = query.filter(MarketOrder.status == status)

    orders = query.order_by(MarketOrder.created_at.desc()).all()

    return jsonify([{
        "id": o.id,
        "status": o.status,
        "total": float(o.total_monetary or 0),
        "created_at": o.created_at.isoformat(),
        "contact_name": o.contact_name
    } for o in orders])


@market_admin_bp.put("/orders/<int:order_id>")
@token_requerido
@require_role("admin", "super_admin")
def admin_update_order(current_user, order_id):
    payload = request.get_json(silent=True) or {}
    tenant = _resolve_admin_tenant(current_user, payload)

    order = MarketOrder.legacy_safe_query().filter_by(id=order_id, tenant_id=tenant.id).first()
    if not order:
        return jsonify({"error": "Order not found"}), 404

    new_status = payload.get('status')
    if new_status:
        previous_status = order.status
        order.status = new_status
        db.session.add(
            OrderEvent(
                market_order_id=order.id,
                type="status_changed",
                payload={
                    "previous_status": previous_status,
                    "status": new_status,
                    "message": f"Estado actualizado de {previous_status} a {new_status}",
                },
            )
        )
        # Hook for notification
        dispatch_order_update(order, f"Tu pedido #{order.id} cambió a estado: {new_status}")

    db.session.commit()
    emit_tenant_update(tenant.slug, 'order_update', {"id": order.id, "status": order.status})
    return jsonify({"id": order.id, "status": order.status})
