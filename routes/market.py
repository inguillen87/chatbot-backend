from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional
from urllib.parse import quote

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    request,
    g,
    session,
)
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
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


market_bp = Blueprint("market", __name__, url_prefix="/api/market")
market_public_bp = Blueprint("market_public", __name__, url_prefix="/market")
market_public_alias_bp = Blueprint("market_public_alias", __name__)
market_admin_bp = Blueprint("market_admin", __name__, url_prefix="/api/admin/market")


def _resolve_tenant(slug: str) -> TenantProfile:
    slug_clean = _canonical_slug(slug)
    if not slug_clean:
        abort(400, description="Slug requerido")

    try:
        # Reutilizamos el mismo resolver multitenant usado en el resto de la app
        # para respetar aliases y configuraciones de dominio.
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
        fallback_slug = _canonical_slug(
            (current_app.config or {}).get("MARKETPLACE_DEFAULT_TENANT")
        )
        if fallback_slug and fallback_slug != slug_clean:
            tenant = (
                TenantProfile.query.filter(func.lower(TenantProfile.slug) == fallback_slug)
                .order_by(TenantProfile.id.desc())
                .first()
            )

    if tenant is None:
        abort(404, description="Tenant no encontrado")

    g.tenant_profile = tenant
    g.tenant_profile_slug = tenant.slug
    return tenant


def _tenant_owner(tenant: TenantProfile) -> Optional[User]:
    return tenant.municipio or tenant.pyme


def _canonical_slug(slug: Optional[str]) -> Optional[str]:
    slug_clean = (slug or "").strip().lower()
    if not slug_clean:
        return slug_clean

    alias_map = {
        "municipio": "municipalidad-de-junin",
        "muni": "municipalidad-de-junin",
        "municipalidad": "municipalidad-de-junin",
        "market": "municipalidad-de-junin",
    }

    resolved = alias_map.get(slug_clean, slug_clean)
    default_hint = (current_app.config or {}).get(
        "MARKETPLACE_DEFAULT_TENANT", "municipalidad-de-junin"
    )

    # Permite que el slug "market" sin contexto apunte a un tenant demo configurable.
    if resolved in {"market", "demo"}:
        resolved = default_hint

    return resolved


def _ensure_session_id() -> str:
    session_id = session.get("market_session_id")
    if not session_id:
        import secrets

        session_id = secrets.token_hex(16)
        session["market_session_id"] = session_id
        session.modified = True
    return session_id


def _user_can_manage_tenant(user: User, tenant: TenantProfile) -> bool:
    tenant_owner_ids = {tenant.municipio_id, tenant.pyme_id}
    if user.id in tenant_owner_ids:
        return True

    # Staff members pueden tener empresa_id/municipio_id o un slug asociado al tenant.
    if getattr(user, "empresa_id", None) and user.empresa_id in tenant_owner_ids:
        return True
    if getattr(user, "municipio_id", None) and user.municipio_id in tenant_owner_ids:
        return True
    if getattr(user, "tenant_slug", None) and user.tenant_slug == tenant.slug:
        return True
    return False


def _resolve_tenant_soft(slug: str) -> Optional[TenantProfile]:
    slug_clean = _canonical_slug(slug)
    if not slug_clean:
        return None

    try:
        return resolve_tenant_only(tenant_slug=slug_clean, require_explicit_slug=False)
    except TenantResolutionError:
        pass

    tenant = (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug_clean)
        .order_by(TenantProfile.id.desc())
        .first()
    )

    if tenant is None:
        fallback_slug = _canonical_slug(
            (current_app.config or {}).get("MARKETPLACE_DEFAULT_TENANT")
        )
        if fallback_slug and fallback_slug != slug_clean:
            tenant = (
                TenantProfile.query.filter(func.lower(TenantProfile.slug) == fallback_slug)
                .order_by(TenantProfile.id.desc())
                .first()
            )

    return tenant


def _modalidad_value(producto: CatalogoItem) -> str:
    modalidad = getattr(producto, "modalidad", None)
    try:
        from enum import Enum

        if isinstance(modalidad, Enum):
            return modalidad.value
    except Exception:
        pass

    if modalidad:
        return str(modalidad)

    return "venta"


def _resolve_admin_tenant(current_user: User, payload: Optional[dict] = None) -> TenantProfile:
    payload = payload or {}
    tenant = None

    tenant_id = payload.get("tenant_id") or request.args.get("tenant_id")
    tenant_slug = (
        payload.get("tenant_slug")
        or request.args.get("tenant_slug")
        or request.args.get("slug")
        or getattr(current_user, "tenant_slug", None)
    )

    if tenant_id:
        try:
            tenant = TenantProfile.query.get(int(tenant_id))
        except (TypeError, ValueError):
            tenant = None

    if tenant is None and tenant_slug:
        try:
            tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=False)
        except TenantResolutionError:
            tenant = None

    if tenant is None:
        # Último recurso: si el usuario es dueño de un municipio/pyme usamos ese tenant.
        owner_id = getattr(current_user, "municipio_id", None) or getattr(current_user, "empresa_id", None)
        if owner_id:
            tenant = TenantProfile.query.filter(
                (TenantProfile.municipio_id == owner_id) | (TenantProfile.pyme_id == owner_id)
            ).first()

    if tenant is None:
        abort(404, description="Tenant no encontrado")

    if not _user_can_manage_tenant(current_user, tenant):
        abort(403, description="No autorizado para este tenant")

    return tenant


def _public_market_base(tenant: TenantProfile) -> tuple[str, str, str]:
    """Return (full_url, base_url, path) for the tenant's public market."""

    return _build_public_cart_url(tenant)


def _whatsapp_share_link(text: str) -> str:
    return f"https://wa.me/?text={quote(text)}"


def _serialize_catalog_item(
    item: CatalogoItem, tenant: Optional[TenantProfile] = None
) -> Dict[str, object]:
    price_value = None
    try:
        if item.precio_monetario is not None:
            price_value = float(item.precio_monetario)
    except (TypeError, ValueError):
        price_value = None

    public_url = None
    whatsapp_share = None
    if tenant:
        full_url, _, path = _public_market_base(tenant)
        product_path = f"/catalog/{item.id}" if path != "override" else ""
        public_url = f"{full_url}{product_path}" if full_url else None
        if public_url:
            share_text = (
                f"Mirá {item.nombre} en el marketplace de {tenant.nombre or tenant.slug}: {public_url}"
            )
            whatsapp_share = _whatsapp_share_link(share_text)

    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "nombre": item.nombre,
        "descripcion": item.descripcion,
        "precio": price_value,
        "precio_texto": item.precio,
        "moneda": item.moneda,
        "imagen_url": item.imagen_url,
        "pdf_url": getattr(item, "pdf_url", None),
        "categoria": item.categoria,
        "unidad": item.unidad,
        "cantidad": item.cantidad,
        "sku": item.sku,
        "marca": item.marca,
        "modalidad": _modalidad_value(item),
        "puntos": item.precio_puntos,
        "disponible": bool(getattr(item, "disponible", True)),
        "descripcion_corta": getattr(item, "descripcion_corta", None),
        "promocion_info": getattr(item, "promocion_info", None),
        "public_url": public_url,
        "whatsapp_share_url": whatsapp_share,
    }

def _get_or_create_cart_for_user(
    tenant: TenantProfile, user: User, *, create_if_missing: bool = False
) -> Optional[MarketCart]:
    """Return the open cart for the given tenant/user, optionally creating it.

    Always scopes by ``tenant_id`` and ``user_id`` to avoid mezclar productos de
    distintos tenants. When ``create_if_missing`` is False, no new cart is
    persisted (útil para GET /cart o para validar estado tras checkout).
    """

    if not user or not getattr(user, "is_authenticated", False):
        abort(401, description="Autenticación requerida")

    # Check for anonymous cart to merge/claim
    session_id = _ensure_session_id()
    anon_cart = (
        MarketCart.query.filter(
            MarketCart.tenant_id == tenant.id,
            MarketCart.status == "open",
            MarketCart.session_id == session_id,
            MarketCart.user_id.is_(None),
        )
        .order_by(MarketCart.updated_at.desc())
        .first()
    )

    user_cart = (
        MarketCart.query.filter(
            MarketCart.tenant_id == tenant.id,
            MarketCart.status == "open",
            MarketCart.user_id == user.id,
        )
        .order_by(MarketCart.updated_at.desc())
        .first()
    )

    if anon_cart:
        if user_cart:
            # Merge items from anon_cart to user_cart
            for anon_item in anon_cart.items:
                existing_item = user_cart.items.filter_by(product_id=anon_item.product_id).first()
                if existing_item:
                    existing_item.quantity += anon_item.quantity
                else:
                    anon_item.cart_id = user_cart.id
            db.session.delete(anon_cart)
            cart = user_cart
        else:
            # Claim anon_cart
            anon_cart.user_id = user.id
            cart = anon_cart
        db.session.commit()
    else:
        cart = user_cart

    if cart is None and create_if_missing:
        cart = MarketCart(
            tenant_id=tenant.id,
            user_id=user.id,
            session_id=session_id,
            contact_phone=getattr(user, "telefono", None),
            contact_name=getattr(user, "name", None),
        )
        db.session.add(cart)
        db.session.commit()

    stored_carts = session.get("market_cart_ids") or {}
    if cart and stored_carts.get(tenant.slug) != cart.id:
        stored_carts[tenant.slug] = cart.id
        session["market_cart_ids"] = stored_carts
        session.modified = True

    return cart


def _product_query_for_tenant(owner: User, tenant: TenantProfile, *, include_unavailable: bool = False):
    filters = [
        or_(CatalogoItem.tenant_id == tenant.id, CatalogoItem.tenant_id.is_(None)),
        CatalogoItem.user_id == owner.id,
    ]

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


def _empty_cart_summary(tenant: TenantProfile) -> Dict[str, object]:
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
    }


@market_bp.get("/<slug>/catalog")
def public_catalog(slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

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


@market_bp.get("/<slug>/catalog/<int:product_id>")
def public_product_detail(slug: str, product_id: int):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

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
@token_requerido
def public_cart_summary(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

    ensure_seed_catalog(owner, tenant)
    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify(_empty_cart_summary(tenant))

    summary = _cart_summary(cart, owner)
    summary["ui_signals"] = {"animation": "cart-burst"}
    return jsonify(summary)


@market_bp.post("/<slug>/cart/add")
@token_requerido
def public_cart_add(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

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
@token_requerido
def public_cart_remove(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

    payload = request.get_json(silent=True) or {}
    item_id = payload.get("product_id") or payload.get("catalogo_item_id") or payload.get("item_id")
    try:
        item_id_int = int(item_id)
    except (TypeError, ValueError):
        return jsonify({"error": "catalogo_item_id requerido"}), 400

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify({"error": "Carrito no encontrado"}), 404

    cart_item = cart.items.filter(MarketCartItem.product_id == item_id_int).first()
    if cart_item is None:
        return jsonify({"error": "Item no encontrado en el carrito"}), 404

    db.session.delete(cart_item)
    db.session.commit()
    return jsonify(_cart_summary(cart, owner))


@market_bp.post("/<slug>/cart/clear")
@token_requerido
def public_cart_clear(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify(_empty_cart_summary(tenant))
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400

    for entry in list(cart.items.all()):
        db.session.delete(entry)

    cart.estado = "abierto"
    db.session.commit()
    return jsonify(_cart_summary(cart, owner))


@market_bp.post("/<slug>/cart/checkout")
@token_requerido
def public_cart_checkout(current_user, slug: str):
    tenant = _resolve_tenant(slug)
    owner = _tenant_owner(tenant)
    if owner is None:
        abort(404, description="Tenant sin propietario")

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
        abort(404, description="Tenant sin propietario")

    cart = _get_or_create_cart_for_user(tenant, current_user, create_if_missing=False)
    if cart is None:
        return jsonify({"error": "Carrito no encontrado"}), 404
    if cart.status != "open":
        return jsonify({"error": "El carrito ya está confirmado o cancelado"}), 400
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

    whatsapp_share = None
    if full_url:
        share_text = f"Entrá al carrito de {tenant.nombre or tenant.slug}: {full_url}"
        whatsapp_share = _whatsapp_share_link(share_text)

    return jsonify(
        {
            "cart_url": full_url,
            "base_url": base_url,
            "tenant_slug": tenant.slug,
            "path": path,
            "whatsapp_share_url": whatsapp_share,
        }
    )


@market_public_bp.route("/<slug>/catalog", methods=["GET", "OPTIONS"])
def public_catalog_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_catalog(slug)


@market_public_bp.route("/<slug>/catalog/<int:product_id>", methods=["GET", "OPTIONS"])
def public_product_detail_alias(slug: str, product_id: int):
    if request.method == "OPTIONS":
        return "", 204
    return public_product_detail(slug, product_id)


@market_public_bp.route("/<slug>/cart", methods=["GET", "OPTIONS"])
def public_cart_summary_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_summary(slug)


@market_public_bp.route("/<slug>/cart/add", methods=["POST", "OPTIONS"])
def public_cart_add_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_add(slug)


@market_public_bp.route("/<slug>/cart/remove", methods=["POST", "OPTIONS"])
def public_cart_remove_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_remove(slug)


@market_public_bp.route("/<slug>/cart/clear", methods=["POST", "OPTIONS"])
def public_cart_clear_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_clear(slug)


@market_public_bp.route("/<slug>/cart/checkout", methods=["POST", "OPTIONS"])
def public_cart_checkout_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_checkout(slug)


@market_public_bp.route("/<slug>/checkout/start", methods=["POST", "OPTIONS"])
def start_checkout_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return start_checkout(slug)


@market_public_bp.route("/<slug>/cart/url", methods=["GET", "OPTIONS"])
def public_cart_url_alias(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_url(slug)


@market_public_alias_bp.route("/<slug>/catalog", methods=["GET", "OPTIONS"])
def public_catalog_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_catalog(slug)


@market_public_alias_bp.route("/<slug>/catalog/<int:product_id>", methods=["GET", "OPTIONS"])
def public_product_detail_root(slug: str, product_id: int):
    if request.method == "OPTIONS":
        return "", 204
    return public_product_detail(slug, product_id)


@market_public_alias_bp.route("/<slug>/cart", methods=["GET", "OPTIONS"])
def public_cart_summary_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_summary(slug)


@market_public_alias_bp.route("/<slug>/cart/add", methods=["POST", "OPTIONS"])
def public_cart_add_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_add(slug)


@market_public_alias_bp.route("/<slug>/cart/remove", methods=["POST", "OPTIONS"])
def public_cart_remove_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_remove(slug)


@market_public_alias_bp.route("/<slug>/cart/clear", methods=["POST", "OPTIONS"])
def public_cart_clear_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_clear(slug)


@market_public_alias_bp.route("/<slug>/cart/checkout", methods=["POST", "OPTIONS"])
def public_cart_checkout_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_checkout(slug)


@market_public_alias_bp.route("/<slug>/checkout/start", methods=["POST", "OPTIONS"])
def start_checkout_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return start_checkout(slug)


@market_public_alias_bp.route("/<slug>/cart/url", methods=["GET", "OPTIONS"])
def public_cart_url_root(slug: str):
    if request.method == "OPTIONS":
        return "", 204
    return public_cart_url(slug)


@market_bp.route("/pwa/public/<tenant_slug>/productos", methods=["GET", "OPTIONS"])
def market_pwa_public_productos(tenant_slug: str):
    """Alias legacy para exponer productos del catálogo por tenant."""

    if request.method == "OPTIONS":
        return "", 204

    tenant = _resolve_tenant_soft(tenant_slug)
    if tenant is None:
        return jsonify({"error": "tenant_not_found"}), 404

    owner = tenant.municipio or tenant.pyme
    if owner:
        ensure_seed_catalog(owner, tenant)

    query = CatalogoItem.query.options(*CatalogoItem.legacy_safe_options()).filter(
        CatalogoItem.tenant_id == tenant.id
    )

    items: list[dict] = []
    for producto in query.all():
        precio_raw = getattr(producto, "precio", None) or getattr(
            producto, "precio_monetario", None
        )
        _, precio_float, _ = parse_precio_flexible(precio_raw)
        items.append(
            {
                "id": producto.id,
                "nombre": producto.nombre,
                "descripcion": producto.descripcion,
                "precio": float(precio_float) if precio_float is not None else None,
                "precio_str": getattr(producto, "precio", None),
                "modalidad": _modalidad_value(producto),
                "puntos": getattr(producto, "puntos", 0) or 0,
                "imagen_url": getattr(producto, "imagen_url", None),
                "categoria": getattr(producto, "categoria", None),
            }
        )

    return jsonify(items)


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
    precio_puntos = payload.get("puntos") or payload.get("price_points")
    if precio_texto is not None:
        try:
            precio_decimal = Decimal(str(precio_texto))
        except (InvalidOperation, ValueError):
            return jsonify({"error": "precio inválido"}), 400
    if precio_puntos is not None:
        try:
            precio_puntos = int(precio_puntos)
        except (TypeError, ValueError):
            return jsonify({"error": "puntos inválidos"}), 400

    producto = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre=nombre,
        descripcion=payload.get("descripcion") or payload.get("description"),
        precio=str(precio_texto) if precio_texto is not None else None,
        precio_monetario=precio_decimal,
        precio_puntos=precio_puntos,
        moneda=(payload.get("moneda") or payload.get("currency") or "ARS").upper(),
        categoria=payload.get("categoria"),
        imagen_url=payload.get("imagen_url") or payload.get("image_url"),
        pdf_url=payload.get("pdf_url"),
        modalidad=payload.get("modalidad") or payload.get("mode") or "venta",
        unidad=payload.get("unidad"),
        cantidad=payload.get("cantidad"),
        sku=payload.get("sku"),
        marca=payload.get("marca"),
        descripcion_corta=payload.get("descripcion_corta") or payload.get("short_description"),
        promocion_info=payload.get("promocion_info") or payload.get("promo"),
        disponible=bool(payload.get("disponible", True)),
    )
    db.session.add(producto)
    db.session.commit()
    return jsonify(_serialize_catalog_item(producto, tenant)), 201


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
    if "imagen_url" in payload or "image_url" in payload:
        producto.imagen_url = payload.get("imagen_url") or payload.get("image_url")
    if "pdf_url" in payload:
        producto.pdf_url = payload.get("pdf_url")
    if "moneda" in payload or "currency" in payload:
        producto.moneda = (payload.get("moneda") or payload.get("currency") or producto.moneda or "ARS").upper()
    if "disponible" in payload:
        producto.disponible = bool(payload.get("disponible"))
    if "modalidad" in payload or "mode" in payload:
        producto.modalidad = payload.get("modalidad") or payload.get("mode") or producto.modalidad
    if "unidad" in payload:
        producto.unidad = payload.get("unidad")
    if "cantidad" in payload:
        producto.cantidad = payload.get("cantidad")
    if "sku" in payload:
        producto.sku = payload.get("sku")
    if "marca" in payload:
        producto.marca = payload.get("marca")
    if "descripcion_corta" in payload or "short_description" in payload:
        producto.descripcion_corta = payload.get("descripcion_corta") or payload.get("short_description")
    if "promocion_info" in payload or "promo" in payload:
        producto.promocion_info = payload.get("promocion_info") or payload.get("promo")

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
    if "puntos" in payload or "price_points" in payload:
        puntos_val = payload.get("puntos") or payload.get("price_points")
        if puntos_val is None:
            producto.precio_puntos = None
        else:
            try:
                producto.precio_puntos = int(puntos_val)
            except (TypeError, ValueError):
                return jsonify({"error": "puntos inválidos"}), 400

    db.session.commit()
    return jsonify(_serialize_catalog_item(producto, tenant))


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
    return jsonify({"deleted": product_id, "tenant_id": tenant.id})
