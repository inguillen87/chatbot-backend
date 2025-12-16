from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import logging

from flask import Blueprint, g, jsonify, request, session, abort, make_response
from flask_cors import cross_origin
from flask_login import current_user
from sqlalchemy import func, or_

from config import ALLOWED_ORIGINS
from database import db
from middleware import require_tenant
from models import (
    CatalogoItem,
    MarketCart,
    MarketCartItem,
    TenantProfile,
    User,
    CatalogoModalidad
)
from routes.catalogo import _formatear_producto
from routes.productos import (
    _lookup_tenant_by_slug,
    _resolve_authenticated_user,
    _tenant_for_user,
    _tenant_slug_from_url,
)
from services.catalog_seed import ensure_seed_catalog
from services.common_utils import parse_precio_flexible
from services.rewards_demo import reward_profile_for_tenant
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
from utils.tenant import get_current_tenant_profile

carrito_bp = Blueprint('carrito_bp', __name__, url_prefix='/carrito')
logger = logging.getLogger(__name__)

_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "x-anon-id",
    "anon-id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]

_CORS_EXPOSE_HEADERS = ["Content-Type", "Authorization", "X-Anon-Id", "Anon-Id"]

def _cors_kwargs(methods: list[str]) -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _CORS_ALLOWED_HEADERS,
        "expose_headers": _CORS_EXPOSE_HEADERS,
        "methods": methods,
    }

def _tenant_missing_response():
    return (
        jsonify(
            {
                "error": "Tenant requerido para carrito",
                "detail": (
                    "No se pudo resolver el municipio/pyme del widget. "
                    "Incluí el header X-Tenant o X-Widget-Token, o el parámetro ?tenant= para continuar."
                ),
                "action": "select_tenant",
            }
        ),
        400,
    )

def _resolve_session_identifier() -> str:
    """Resolve a stable identifier for the cart owner.

    Prioritizes the explicit `X-Anon-Id` header (or cookie) sent by the
    frontend, which persists across browser sessions better than the Flask
    session cookie. Falls back to the Flask session if no anonymous ID is provided.
    """
    chat_session_id = (
        request.headers.get("X-Chat-Session-Id")
        or request.headers.get("Chat-Session-Id")
        or request.args.get("chat_session_id")
    )
    if not chat_session_id:
        body = request.get_json(silent=True) or {}
        chat_session_id = body.get("chat_session_uuid") or body.get("chatSessionId")
    if chat_session_id:
        return str(chat_session_id)

    # 1. Try Anon-Id from headers (most reliable for PWA/Widgets)
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.headers.get("x-anon-id")
        or request.headers.get("anon-id")
    )
    if anon_id:
        return anon_id

    # 2. Try cookie (if standard web client) - Prioritize 'chatboc_anon_id' as seen in logs
    anon_id_cookie = request.cookies.get("chatboc_anon_id") or request.cookies.get("anon_id")
    if anon_id_cookie:
        return anon_id_cookie

    # 3. Fallback to flask session (volatile if cookies blocked)
    session_id = session.get("market_session_id")
    if not session_id:
        import secrets
        session_id = secrets.token_hex(16)
        session["market_session_id"] = session_id
        session.modified = True
    return session_id

def _get_or_create_db_cart(
    tenant: TenantProfile,
    user: Optional[User] = None,
    *,
    create_if_missing: bool = True,
) -> Optional[MarketCart]:
    """Return the open MarketCart for the given tenant/user combination."""

    session_id = _resolve_session_identifier()
    user_id = getattr(user, "id", None) if user and user.is_authenticated else None

    base_query = MarketCart.query.filter(
        MarketCart.tenant_id == tenant.id,
        MarketCart.status == "open",
    )

    cart = None
    # 1. Try to find by User ID if authenticated
    if user_id:
        cart = (
            base_query.filter(MarketCart.user_id == user_id)
            .order_by(MarketCart.updated_at.desc())
            .first()
        )

    # 2. Try to find by Session ID
    if cart is None:
        cart = (
            base_query.filter(MarketCart.session_id == session_id)
            .order_by(MarketCart.updated_at.desc())
            .first()
        )

    # 3. Create if missing
    if cart is None:
        if not create_if_missing:
            return None

        cart = MarketCart(
            tenant_id=tenant.id,
            user_id=user_id,
            session_id=session_id,
            contact_phone=getattr(user, "telefono", None) if user else None,
            contact_name=getattr(user, "name", None) if user else None,
        )
        db.session.add(cart)
        db.session.commit()
    else:
        # Update session/user association if needed
        modified = False
        if cart.session_id != session_id:
            cart.session_id = session_id
            modified = True
        if user_id and cart.user_id is None:
            cart.user_id = user_id
            modified = True
        if modified:
            db.session.commit()

    return cart

def _product_query_for_tenant(owner: User, tenant: TenantProfile):
    filters = [CatalogoItem.tenant_id == tenant.id, CatalogoItem.user_id == owner.id]
    # Include unavailable items to show in cart (maybe disabled later) but primarily we filter available in catalog
    filters.append(or_(CatalogoItem.disponible.is_(True), CatalogoItem.disponible.is_(None)))
    return CatalogoItem.query.options(*CatalogoItem.legacy_safe_options()).filter(*filters)

def _pricing_snapshot(product: CatalogoItem) -> Dict[str, object]:
    formatted = _formatear_producto({
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
    })

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

def _db_cart_summary(cart: MarketCart, owner: User, *, event: Optional[str] = None) -> Dict[str, object]:
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
    total_points = 0.0
    total_count = 0
    total_monetary_accum = 0.0

    for entry in items:
        product = products.get(entry.product_id)
        formatted = _pricing_snapshot(product) if product else None

        # Fallback values from entry if product deleted
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
                subtotal_points = float(price_points) * entry.quantity
                total_points += subtotal_points
        elif price_monetary is not None:
            subtotal = float(price_monetary) * entry.quantity
            totals_by_currency[currency] = totals_by_currency.get(currency, 0.0) + subtotal
            if currency == "ARS": # Assumption for total_estimado legacy field
                total_monetary_accum += subtotal

        total_count += entry.quantity

        item_data = {
            "catalogo_item_id": entry.product_id,
            "product_id": entry.product_id, # Alias for some frontends
            "id": entry.product_id, # Alias for some frontends
            "nombre": product.nombre if product else entry.name_snapshot,
            "cantidad": entry.quantity,
            "quantity": entry.quantity, # Alias for JS
            "precio_unitario": price_monetary,
            "precio_unitario_texto": price_text,
            "subtotal": subtotal,
            "subtotal_puntos": subtotal_points,
            "moneda": currency,
            "modalidad": modalidad,
            "imagen_url": formatted["formatted"].get("imagen_url") if formatted else None,
            "descripcion": formatted["formatted"].get("descripcion") if formatted else None,
            "categoria": formatted["formatted"].get("categoria") if formatted else None,
        }

        # Add unit/quantity_label to avoid ReferenceError on frontend
        if product and product.unidad:
            item_data["unidad"] = product.unidad
            item_data["quantityLabel"] = product.unidad
            item_data["quantity_label"] = product.unidad
        else:
            item_data["unidad"] = "u"
            item_data["quantityLabel"] = "u"
            item_data["quantity_label"] = "u"

        enriched.append(item_data)

    # Legacy fields + New fields
    resumen = {
        "tenant_id": cart.tenant_id,
        "cart_id": cart.id,
        "items": enriched,
        "items_count": total_count,
        "totales_monedas": {k: round(v, 2) for k, v in totals_by_currency.items()},
        "total_estimado": round(total_monetary_accum, 2),
        "total_puntos_estimado": total_points,
        "moneda": "ARS", # Legacy default
        "badge_count": total_count,
        "checkout_options": {
            "mercadopago_ready": True,
            "gateway_hint": "Mercado Pago preference/token flow listo para demo",
            "points_enabled": total_points > 0,
        },
        "ui_signals": {
            "event": event or "refresh",
            "animation": "cart-burst" if event else "soft-pulse",
            "badge": total_count, # Legacy expects direct value sometimes
            "toast": "Carrito actualizado",
        },
    }

    resumen["recompensas_demo"] = reward_profile_for_tenant(cart.tenant_id, total_points)
    return resumen

def _resolve_owner_and_seed() -> Tuple[Optional[TenantProfile], Optional[User]]:
    """Resolves tenant and owner using standard middleware or headers."""
    user = _resolve_authenticated_user()
    tenant = _tenant_for_user(user)

    if not tenant:
        tenant = getattr(g, "tenant_profile", None) or get_current_tenant_profile()

    if not tenant:
        # Fallback to headers if not set by middleware
        tenant_slug_hint = (
            request.headers.get("X-Tenant")
            or request.args.get("tenant_slug")
            or request.args.get("tenant")
            or _tenant_slug_from_url(request.referrer)
        )
        if tenant_slug_hint:
            try:
                tenant = resolve_tenant_only(tenant_slug=tenant_slug_hint, require_explicit_slug=True)
            except TenantResolutionError:
                pass

    owner = None
    if tenant:
        owner = tenant.municipio or tenant.pyme or user
        # Seed catalog if needed
        ensure_seed_catalog(owner, tenant)
        g.tenant_profile = tenant # Cache for this request

    return tenant, owner

@carrito_bp.route('', methods=['GET', 'POST', 'OPTIONS'])
@carrito_bp.route('/', methods=['GET', 'POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["GET", "POST", "OPTIONS"]))
@require_tenant
def carrito_root():
    if request.method == 'OPTIONS':
        return "", 204
    if request.method == 'GET':
        return resumen()
    return agregar()

@carrito_bp.route('/agregar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
@require_tenant
def agregar():
    if request.method == 'OPTIONS':
        return "", 204

    tenant, owner = _resolve_owner_and_seed()
    if not tenant or not owner:
        return _tenant_missing_response()

    payload = request.get_json(silent=True) or {}
    session_debug = _resolve_session_identifier()
    logger.debug(f"Carrito Add Payload: {payload} | SessionID: {session_debug}")

    # Support multiple formats
    item_id = payload.get('catalogo_item_id') or payload.get('item_id') or payload.get('product_id') or payload.get('id') or payload.get('producto_id')
    cantidad = 1
    try:
        cantidad = int(payload.get('cantidad', 1))
        if cantidad < 1: cantidad = 1
    except:
        pass

    if not item_id:
        return jsonify({'error': 'catalogo_item_id requerido'}), 400

    product = _product_query_for_tenant(owner, tenant).filter(CatalogoItem.id == item_id).first()
    if not product:
        # Fallback for demo mock matching by string ID if needed?
        # But ensure_seed_catalog should have created integer IDs.
        return jsonify({'error': 'Producto no encontrado'}), 404

    cart = _get_or_create_db_cart(tenant, current_user, create_if_missing=True)
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400

    cart_item = cart.items.filter(MarketCartItem.product_id == product.id).first()
    pricing = _pricing_snapshot(product)

    if cart_item:
        cart_item.quantity += cantidad
    else:
        cart_item = MarketCartItem(
            cart_id=cart.id,
            product_id=product.id,
            quantity=cantidad,
            price_text=pricing.get("price_text"),
            price_monetary=pricing.get("price_monetary"),
            price_points=pricing.get("price_points"),
            currency=pricing.get("currency"),
            modalidad=pricing.get("modalidad"),
            name_snapshot=product.nombre,
        )
        db.session.add(cart_item)

    db.session.commit()
    return jsonify(_db_cart_summary(cart, owner, event="add"))

@carrito_bp.route('/actualizar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
@require_tenant
def actualizar():
    if request.method == 'OPTIONS':
        return "", 204

    tenant, owner = _resolve_owner_and_seed()
    if not tenant or not owner:
        return _tenant_missing_response()

    payload = request.get_json(silent=True) or {}
    item_id = payload.get('catalogo_item_id') or payload.get('item_id') or payload.get('product_id') or payload.get('id') or payload.get('producto_id')
    try:
        cantidad = int(payload.get('cantidad', 0))
    except:
        return jsonify({'error': 'Cantidad inválida'}), 400

    if not item_id:
        return jsonify({'error': 'catalogo_item_id requerido'}), 400

    cart = _get_or_create_db_cart(tenant, current_user, create_if_missing=False)
    if not cart:
        return jsonify({'error': 'Carrito no encontrado'}), 404

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id).first()
    if not cart_item:
        return jsonify({'error': 'Item no encontrado en el carrito'}), 404

    if cantidad <= 0:
        db.session.delete(cart_item)
    else:
        cart_item.quantity = cantidad

    db.session.commit()
    return jsonify(_db_cart_summary(cart, owner, event="update"))

@carrito_bp.route('/eliminar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
@require_tenant
def eliminar():
    if request.method == 'OPTIONS':
        return "", 204

    tenant, owner = _resolve_owner_and_seed()
    if not tenant or not owner:
        return _tenant_missing_response()

    payload = request.get_json(silent=True) or {}
    item_id = payload.get('catalogo_item_id') or payload.get('item_id') or payload.get('product_id') or payload.get('id') or payload.get('producto_id')

    if not item_id:
        return jsonify({'error': 'catalogo_item_id requerido'}), 400

    cart = _get_or_create_db_cart(tenant, current_user, create_if_missing=False)
    if not cart:
        return jsonify({'error': 'Carrito no encontrado'}), 404

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id).first()
    if cart_item:
        db.session.delete(cart_item)
        db.session.commit()

    return jsonify(_db_cart_summary(cart, owner, event="remove"))

@carrito_bp.route('/vaciar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
@require_tenant
def vaciar():
    if request.method == 'OPTIONS':
        return "", 204

    tenant, owner = _resolve_owner_and_seed()
    if not tenant or not owner:
        return _tenant_missing_response()

    cart = _get_or_create_db_cart(tenant, current_user, create_if_missing=False)
    if cart:
        for item in list(cart.items.all()):
            db.session.delete(item)
        db.session.commit()
        return jsonify(_db_cart_summary(cart, owner, event="clear"))

    # Return empty summary even if cart didn't exist
    return jsonify({
        "tenant_id": tenant.id,
        "items": [],
        "items_count": 0,
        "total_estimado": 0.0,
        "badge_count": 0,
        "recompensas_demo": reward_profile_for_tenant(tenant.id, 0.0),
        "ui_signals": {"event": "clear", "animation": "cart-burst", "badge": 0, "toast": "Carrito vaciado"},
        "checkout_options": {"points_enabled": False}
    })

@carrito_bp.route('/resumen', methods=['GET'])
@cross_origin(**_cors_kwargs(["GET"]))
@require_tenant
def resumen():
    tenant, owner = _resolve_owner_and_seed()
    if not tenant or not owner:
        return _tenant_missing_response()

    cart = _get_or_create_db_cart(tenant, current_user, create_if_missing=False)
    if not cart:
        # Empty response
        return jsonify({
            "tenant_id": tenant.id,
            "items": [],
            "items_count": 0,
            "total_estimado": 0.0,
            "badge_count": 0,
            "recompensas_demo": reward_profile_for_tenant(tenant.id, 0.0),
            "ui_signals": {"event": "refresh", "animation": "idle", "badge": 0},
            "checkout_options": {"points_enabled": False}
        })

    return jsonify(_db_cart_summary(cart, owner))

@carrito_bp.route('/pwa/public/<tenant_slug>/carrito', methods=['GET', 'POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["GET", "POST", "OPTIONS"]))
def carrito_pwa_public(tenant_slug: str):
    """Alias legacy para exponer el carrito público por slug."""
    if request.method == 'OPTIONS':
        return "", 204

    tenant, owner = _resolve_public_tenant_by_slug(tenant_slug)
    if not tenant or not owner:
        return jsonify({'error': 'tenant_not_found'}), 404

    # Inject context manually since require_tenant middleware might not have run
    g.tenant_profile = tenant

    if request.method == 'GET':
        return resumen()

    # POST
    return agregar()

def _resolve_public_tenant_by_slug(
    tenant_slug: str,
) -> Tuple[Optional[TenantProfile], Optional[User]]:
    slug_clean = (tenant_slug or "").strip().lower()
    if not slug_clean:
        return None, None

    try:
        tenant = resolve_tenant_only(tenant_slug=slug_clean, require_explicit_slug=False)
    except TenantResolutionError:
        tenant = None

    if tenant is None:
        tenant = _lookup_tenant_by_slug(slug_clean)

    owner = tenant.municipio or tenant.pyme if tenant else None
    if tenant and owner:
        ensure_seed_catalog(owner, tenant)

    return tenant, owner
