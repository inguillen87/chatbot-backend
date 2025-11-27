from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, session
from flask_cors import cross_origin
from sqlalchemy import func

from config import ALLOWED_ORIGINS
from models import CatalogoItem, TenantProfile, User, CatalogoModalidad
from routes.catalogo import _formatear_producto
from routes.productos import _resolve_public_owner, _resolve_authenticated_user
from services.catalog_seed import ensure_seed_catalog
from services.cart import add_item, clear_cart, get_summary, remove_item, update_item
from services.common_utils import parse_precio_flexible
from services.rewards_demo import reward_profile_for_tenant

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

_CORS_EXPOSE_HEADERS = ["Content-Type", "Authorization", "X-Anon-Id", "Anon-Id"]


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


def _cors_kwargs(methods: list[str]) -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _CORS_ALLOWED_HEADERS,
        "expose_headers": _CORS_EXPOSE_HEADERS,
        "methods": methods,
    }


carrito_bp = Blueprint('carrito_bp', __name__, url_prefix='/carrito')


def _get_session_cart_data() -> Dict[str, list]:
    pyme_carts_data = session.get('carritos_pymes')
    if not isinstance(pyme_carts_data, dict):
        pyme_carts_data = {}
        session['carritos_pymes'] = pyme_carts_data
    return pyme_carts_data


def _persist_session_cart_data(pyme_carts_data: Dict[str, list]) -> None:
    session['carritos_pymes'] = pyme_carts_data
    session.modified = True


def _normalize_quantity(value: object, default: int = 1, min_value: int = 1) -> int:
    try:
        cantidad = int(value)
    except (TypeError, ValueError):
        cantidad = default
    return max(cantidad, min_value)


def _cart_key(tenant: Optional[TenantProfile]) -> str:
    return str(getattr(tenant, 'id', 0))


def _tenant_cart(data: Dict[str, list], tenant: Optional[TenantProfile]) -> List[dict]:
    key = _cart_key(tenant)
    cart = data.get(key)
    if not isinstance(cart, list):
        cart = []
        data[key] = cart
    return cart


def _lookup_catalog_item(owner: User, payload: Dict[str, object], tenant: Optional[TenantProfile]) -> Optional[CatalogoItem]:
    identifier = payload.get('catalogo_item_id') or payload.get('item_id')
    sku = payload.get('sku')
    nombre = payload.get('nombre')

    query = CatalogoItem.query.options(*CatalogoItem.legacy_safe_options()).filter(
        CatalogoItem.user_id == owner.id
    )
    if tenant:
        query = query.filter(func.coalesce(CatalogoItem.tenant_id, tenant.id) == tenant.id)
    if identifier is not None:
        try:
            identifier = int(identifier)  # type: ignore[assignment]
        except (TypeError, ValueError):
            identifier = None
        else:
            item = query.filter(CatalogoItem.id == identifier).first()
            if item:
                return item

    if sku:
        sku_text = str(sku).strip().lower()
        if sku_text:
            item = query.filter(func.lower(CatalogoItem.sku) == sku_text).first()
            if item:
                return item

    if nombre:
        nombre_text = str(nombre).strip().lower()
        if nombre_text:
            return query.filter(func.lower(CatalogoItem.nombre) == nombre_text).first()

    return None


def _resolve_owner_and_seed() -> Tuple[Optional[TenantProfile], Optional[User]]:
    user = _resolve_authenticated_user()
    tenant = getattr(user, "tenant_profile", None) if user else None
    if tenant and user:
        ensure_seed_catalog(user, tenant)
        return tenant, user

    tenant, owner = _resolve_public_owner(require_explicit=True)
    if tenant is None or owner is None:
        tenant, owner = _resolve_public_owner(require_explicit=False)
    if tenant is None or owner is None:
        return None, None
    ensure_seed_catalog(owner, tenant)
    return tenant, owner


def _enrich_cart_summary(pyme_carts_data: Dict[str, list], tenant: TenantProfile, owner: User) -> Dict[str, object]:
    cart = list(_tenant_cart(pyme_carts_data, tenant))
    if not cart:
        return {
            'tenant_id': tenant.id,
            'items': [],
            'items_count': 0,
            'total_estimado': 0.0,
            'moneda': 'ARS',
            'badge_count': 0,
            'total_puntos_estimado': 0.0,
            'recompensas_demo': reward_profile_for_tenant(tenant.id, 0.0),
        }

    item_ids = [entry.get('catalogo_item_id') for entry in cart if entry.get('catalogo_item_id')]
    catalog_items: Dict[int, CatalogoItem] = {}
    if item_ids:
        rows = (
            CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
            .filter(
                CatalogoItem.user_id == owner.id,
                func.coalesce(CatalogoItem.tenant_id, tenant.id) == tenant.id,
                CatalogoItem.id.in_(item_ids),
            )
            .all()
        )
        catalog_items = {row.id: row for row in rows}

    enriched = []
    total = 0.0
    total_count = 0
    totals_by_currency: Dict[str, float] = {}
    total_points = 0.0
    for entry in cart:
        item_id = entry.get('catalogo_item_id')
        cantidad = _normalize_quantity(entry.get('cantidad', 1))
        total_count += cantidad
        catalog_item = catalog_items.get(item_id)
        if not catalog_item:
            continue

        formatted = _formatear_producto(
            {
                'nombre': catalog_item.nombre,
                'categoria': catalog_item.categoria,
                'descripcion': catalog_item.descripcion,
                'sku': catalog_item.sku,
                'unidad': catalog_item.unidad,
                'precio_str': catalog_item.precio,
                'cantidad': catalog_item.cantidad,
                'marca': catalog_item.marca,
                'imagen_url': catalog_item.imagen_url,
                'descripcion_corta': catalog_item.descripcion_corta,
                'promocion_info': catalog_item.promocion_info,
            }
        )
        precio_unitario = formatted.get('precio_unitario')
        precio_float = None
        if isinstance(precio_unitario, (int, float)):
            precio_float = float(precio_unitario)
        else:
            _, precio_float, _ = parse_precio_flexible(str(precio_unitario))

        moneda = formatted.get('moneda') or 'ARS'
        modalidad = CatalogoModalidad.from_legacy(formatted.get('modalidad'))
        subtotal = precio_float * cantidad if precio_float is not None else None
        subtotal_puntos = subtotal if moneda == 'PTS' else None
        if modalidad is CatalogoModalidad.DONACION:
            subtotal = None
            subtotal_puntos = None
        if subtotal is not None:
            if moneda == 'PTS' and modalidad is CatalogoModalidad.CANJE:
                total_points += subtotal
            elif moneda != 'PTS':
                totals_by_currency[moneda] = totals_by_currency.get(moneda, 0.0) + subtotal
                total += subtotal

        enriched.append(
            {
                'catalogo_item_id': item_id,
                'nombre': formatted.get('nombre'),
                'descripcion': formatted.get('descripcion'),
                'cantidad': cantidad,
                'precio_unitario': precio_float,
                'precio_unitario_texto': catalog_item.precio,
                'subtotal': subtotal if moneda != 'PTS' else None,
                'subtotal_puntos': subtotal_puntos,
                'imagen_url': formatted.get('imagen_url'),
                'categoria': formatted.get('categoria'),
                'moneda': moneda,
                'modalidad': modalidad.value,
            }
        )

    return {
        'tenant_id': tenant.id,
        'items': enriched,
        'items_count': total_count,
        'total_estimado': round(total, 2),
        'totales_monedas': {k: round(v, 2) for k, v in totals_by_currency.items()},
        'total_puntos_estimado': round(total_points, 2),
        'moneda': 'ARS',
        'badge_count': total_count,
        'recompensas_demo': reward_profile_for_tenant(tenant.id, total_points),
        'checkout_options': {
            'mercadopago_ready': True,
            'gateway_hint': 'Mercado Pago preference/token flow listo para demo',
            'points_enabled': total_points > 0,
        },
        'ui_signals': {
            'animation': 'cart-burst',
            'toast': 'Agregado al carrito',
        },
    }


def _carrito_summary_response():
    tenant, owner = _resolve_owner_and_seed()
    pyme_carts_data = _get_session_cart_data()

    if tenant and owner:
        return jsonify(_enrich_cart_summary(pyme_carts_data, tenant, owner))

    return _tenant_missing_response()


@carrito_bp.route('', methods=['GET', 'POST', 'OPTIONS'])
@carrito_bp.route('/', methods=['GET', 'POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["GET", "POST", "OPTIONS"]))
def carrito_root():
    """Permite consultar el carrito (GET) o agregar items (POST) desde la raíz."""
    if request.method == 'OPTIONS':
        return "", 204

    if request.method == 'GET':
        return _carrito_summary_response()
    return agregar()


@carrito_bp.route('/agregar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
def agregar():
    data = request.get_json(silent=True) or {}
    tenant, owner = _resolve_owner_and_seed()

    if request.method == 'OPTIONS':
        return "", 204

    if not tenant or not owner:
        return _tenant_missing_response()

    if owner:
        item = _lookup_catalog_item(owner, data, tenant)
        if not item:
            return jsonify({'error': 'Producto no encontrado'}), 404

        cantidad = _normalize_quantity(data.get('cantidad', 1))
        pyme_carts_data = _get_session_cart_data()
        cart = _tenant_cart(pyme_carts_data, tenant)
        for entry in cart:
            if entry.get('catalogo_item_id') == item.id:
                entry['cantidad'] = entry.get('cantidad', 0) + cantidad
                break
        else:
            cart.append({'catalogo_item_id': item.id, 'cantidad': cantidad})

        _persist_session_cart_data(pyme_carts_data)
        return _carrito_summary_response()

    # Compatibilidad con datos heredados basados en nombre
    nombre = data.get('nombre')
    cantidad = _normalize_quantity(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    add_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/actualizar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
def actualizar():
    data = request.get_json(silent=True) or {}
    tenant, owner = _resolve_owner_and_seed()

    if request.method == 'OPTIONS':
        return "", 204

    if not tenant or not owner:
        return _tenant_missing_response()

    if owner:
        try:
            item_id = int(data.get('catalogo_item_id') or data.get('item_id'))
        except (TypeError, ValueError):
            item_id = None
        if item_id is None:
            return jsonify({'error': 'catalogo_item_id requerido'}), 400

        cantidad = _normalize_quantity(data.get('cantidad', 0), default=0, min_value=0)
        pyme_carts_data = _get_session_cart_data()
        cart = _tenant_cart(pyme_carts_data, tenant)
        for entry in list(cart):
            if entry.get('catalogo_item_id') == item_id:
                if cantidad <= 0:
                    cart.remove(entry)
                else:
                    entry['cantidad'] = cantidad
                _persist_session_cart_data(pyme_carts_data)
                return _carrito_summary_response()
        return jsonify({'error': 'Item no encontrado en el carrito'}), 404

    nombre = data.get('nombre')
    cantidad = _normalize_quantity(data.get('cantidad', 1), default=1, min_value=0)
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    update_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/eliminar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
def eliminar():
    data = request.get_json(silent=True) or {}
    tenant, owner = _resolve_owner_and_seed()

    if request.method == 'OPTIONS':
        return "", 204

    if not tenant or not owner:
        return _tenant_missing_response()

    if owner:
        try:
            item_id = int(data.get('catalogo_item_id') or data.get('item_id'))
        except (TypeError, ValueError):
            item_id = None
        if item_id is None:
            return jsonify({'error': 'catalogo_item_id requerido'}), 400

        pyme_carts_data = _get_session_cart_data()
        cart = _tenant_cart(pyme_carts_data, tenant)
        removed = False
        for entry in list(cart):
            if entry.get('catalogo_item_id') == item_id:
                cart.remove(entry)
                removed = True
        if removed:
            _persist_session_cart_data(pyme_carts_data)
            return _carrito_summary_response()
        return jsonify({'error': 'Item no encontrado en el carrito'}), 404

    nombre = data.get('nombre')
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    remove_item(pyme_carts_data, nombre)
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/vaciar', methods=['POST', 'OPTIONS'])
@cross_origin(**_cors_kwargs(["POST", "OPTIONS"]))
def vaciar():
    tenant, owner = _resolve_owner_and_seed()

    if request.method == 'OPTIONS':
        return "", 204
    pyme_carts_data = _get_session_cart_data()

    if not tenant or not owner:
        return _tenant_missing_response()

    cart = _tenant_cart(pyme_carts_data, tenant)
    cart.clear()

    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/resumen', methods=['GET'])
@cross_origin(**_cors_kwargs(["GET"]))
def resumen():
    return _carrito_summary_response()

