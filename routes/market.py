from __future__ import annotations

from typing import Dict, List, Optional

from flask import Blueprint, abort, jsonify, make_response, request, g, session
from flask_login import current_user
from sqlalchemy import func, or_

from database import db
from models import (
    CatalogoItem,
    MarketCart,
    MarketCartItem,
    MarketOrder,
    MarketOrderItem,
    TenantProfile,
    User,
)
from routes.catalogo import _formatear_producto
from routes.pwa_public import _build_public_cart_url
from services.catalog_seed import ensure_seed_catalog
from services.common_utils import parse_precio_flexible
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only


market_bp = Blueprint("market", __name__, url_prefix="/api/market")


def _resolve_tenant(slug: str) -> TenantProfile:
    slug_clean = (slug or "").strip().lower()
    if not slug_clean:
        abort(make_response(jsonify({"error": "Slug requerido"}), 400))

    try:
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


def _ensure_session_id() -> str:
    session_id = session.get("market_session_id")
    if not session_id:
        import secrets

        session_id = secrets.token_hex(16)
        session["market_session_id"] = session_id
        session.modified = True
    return session_id


def _require_authenticated_user():
    if not current_user.is_authenticated:
        abort(make_response(jsonify({"error": "Autenticación requerida"}), 401))


def _get_or_create_cart(tenant: TenantProfile) -> MarketCart:
    _require_authenticated_user()

    session_id = _ensure_session_id()
    user_id = current_user.id

    cart = (
        MarketCart.query.filter(
            MarketCart.tenant_id == tenant.id,
            MarketCart.status == "open",
            MarketCart.user_id == user_id,
        )
        .order_by(MarketCart.updated_at.desc())
        .first()
    )

    if cart is None:
        cart = MarketCart(
            tenant_id=tenant.id,
            user_id=user_id,
            session_id=session_id,
            contact_phone=getattr(current_user, "telefono", None),
            contact_name=getattr(current_user, "name", None),
        )
        db.session.add(cart)
        db.session.commit()

    stored_carts = session.get("market_cart_ids") or {}
    if stored_carts.get(tenant.slug) != cart.id:
        stored_carts[tenant.slug] = cart.id
        session["market_cart_ids"] = stored_carts
        session.modified = True

    return cart


def _product_query_for_tenant(owner: User, tenant: TenantProfile):
    return (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter(
            or_(CatalogoItem.tenant_id == tenant.id, CatalogoItem.tenant_id.is_(None)),
            CatalogoItem.user_id == owner.id,
        )
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


def _cart_summary(cart: MarketCart, owner: User) -> Dict[str, object]:
    items = list(cart.items.all())
    product_ids = [item.product_id for item in items if item.product_id]
    products: Dict[int, CatalogoItem] = {}
    if product_ids:
        rows = (
            _product_query_for_tenant(owner, cart.tenant)
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
    return {
        "tenant_id": cart.tenant_id,
        "cart_id": cart.id,
        "items": enriched,
        "items_count": total_count,
        "totales_monedas": {k: round(v, 2) for k, v in totals_by_currency.items()},
        "total_estimado": round(total_default_currency, 2),
        "total_puntos_estimado": total_points,
        "moneda": "ARS",
        "badge_count": total_count,
        "contacto": {
            "nombre": cart.contact_name,
            "telefono": cart.contact_phone,
        },
    }


@market_bp.get("/<slug>/catalog")
def public_catalog(slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    ensure_seed_catalog(owner, tenant)

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


@market_bp.get("/<slug>/cart")
def public_cart_summary(slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    ensure_seed_catalog(owner, tenant)
    cart = _get_or_create_cart(tenant)
    summary = _cart_summary(cart, owner)
    summary["ui_signals"] = {"animation": "cart-burst"}
    return jsonify(summary)


@market_bp.post("/<slug>/cart/add")
def public_cart_add(slug: str):
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

    cart = _get_or_create_cart(tenant)
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
    return jsonify(_cart_summary(cart, owner))


@market_bp.post("/<slug>/cart/remove")
def public_cart_remove(slug: str):
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

    cart = _get_or_create_cart(tenant)
    cart_item = cart.items.filter(MarketCartItem.product_id == item_id_int).first()
    if cart_item is None:
        return jsonify({"error": "Item no encontrado en el carrito"}), 404

    db.session.delete(cart_item)
    db.session.commit()
    return jsonify(_cart_summary(cart, owner))


@market_bp.post("/<slug>/checkout/start")
def start_checkout(slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(make_response(jsonify({"error": "Tenant sin propietario"}), 404))

    cart = _get_or_create_cart(tenant)
    if cart.items.count() == 0:
        return jsonify({"error": "El carrito está vacío"}), 400

    payload = request.get_json(silent=True) or {}
    telefono = payload.get("telefono") or payload.get("phone") or getattr(current_user, "telefono", None)
    nombre = payload.get("nombre") or payload.get("name") or getattr(current_user, "name", None)

    if not telefono:
        return (
            jsonify({"error": "Teléfono requerido para iniciar el checkout"}),
            400,
        )

    cart.contact_phone = telefono
    cart.contact_name = nombre
    if current_user.is_authenticated and cart.user_id is None:
        cart.user_id = current_user.id

    summary = _cart_summary(cart, owner)

    total_monetary = summary.get("total_estimado")
    total_points = summary.get("total_puntos_estimado")
    order = MarketOrder(
        tenant_id=tenant.id,
        user_id=cart.user_id,
        cart_id=cart.id,
        status="pending",
        contact_name=nombre,
        contact_phone=telefono,
        total_monetary=total_monetary,
        total_points=total_points,
        currency="ARS",
        metadata={"totales_monedas": summary.get("totales_monedas", {})},
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
            "total_points": total_points,
            "checkout_options": {
                "mercadopago_ready": True,
                "gateway_hint": "Mercado Pago preference/token flow listo para demo",
                "points_enabled": bool(total_points),
            },
        }
    )


@market_bp.get("/<slug>/cart/url")
def public_cart_url(slug: str):
    tenant = _resolve_tenant(slug)
    full_url, base_url, path = _build_public_cart_url(tenant)
    if path == "override":
        path = f"market/{tenant.slug}/cart"

    return jsonify({"cart_url": full_url, "base_url": base_url, "tenant_slug": tenant.slug, "path": path})
