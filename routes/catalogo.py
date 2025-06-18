from flask import Blueprint, request, jsonify
from models import CatalogoItem
from routes.auth import token_requerido
from services.qdrant_search import buscar_catalogo_qdrant, DEFAULT_SEARCH_LIMIT
from services.utils import calcular_precio_por_unidad

catalogo_bp = Blueprint('catalogo', __name__, url_prefix='/catalogo')


def _formatear_producto(data: dict) -> dict:
    """Normaliza un diccionario de producto al formato universal."""
    precio_pack = None
    precio_unitario = None

    precio_float = data.get("precio_float")
    precio_str = data.get("precio_str")

    if precio_float is not None:
        precio_pack = precio_float
        precio_unitario = calcular_precio_por_unidad(
            precio_float, data.get("unidad") or data.get("presentacion", "")
        )
    elif isinstance(precio_str, str) and precio_str.strip():
        from services.utils import parse_precio_flexible

        _, parsed_float, _ = parse_precio_flexible(precio_str)
        if parsed_float is not None:
            precio_pack = parsed_float
            precio_unitario = calcular_precio_por_unidad(
                parsed_float, data.get("unidad") or data.get("presentacion", "")
            )
        else:
            precio_pack = precio_str.strip()

    if precio_unitario is None:
        precio_unitario = precio_pack

    if isinstance(precio_pack, str) and not precio_pack:
        precio_pack = None

    return {
        "nombre": data.get("nombre", ""),
        "categoria": data.get("categoria") or data.get("categoria_qdrant", ""),
        "descripcion": data.get("descripcion") or None,
        "sku": data.get("sku") or None,
        "presentacion": data.get("unidad") or data.get("presentacion", ""),
        "talles": data.get("talles"),
        "colores": data.get("colores"),
        "precio_unitario": precio_unitario,
        "precio_pack": precio_pack if precio_pack != precio_unitario else None,
        "stock": data.get("cantidad"),
        "marca": data.get("marca"),
        "imagen_url": data.get("imagen_url"),
    }


@catalogo_bp.route('', methods=['GET'])
@token_requerido
def listar_catalogo(user):
    items = CatalogoItem.query.filter_by(user_id=user.id).all()
    if not items:
        return jsonify(
            {
                "mensaje": "No hay productos cargados en el catálogo. "
                "Contactá a la empresa para más info."
            }
        )

    productos = []
    for item in items:
        productos.append(
            _formatear_producto(
                {
                    "nombre": item.nombre,
                    "categoria": item.categoria,
                    "descripcion": item.descripcion,
                    "sku": item.sku,
                    "unidad": item.unidad,
                    "precio_str": item.precio,
                    "cantidad": item.cantidad,
                    "marca": item.marca,
                }
            )
        )
    return jsonify(productos)


@catalogo_bp.route('/buscar', methods=['GET'])
@token_requerido
def buscar_en_catalogo(user):
    consulta = request.args.get('q', '')
    if not consulta:
        return jsonify([])
    resultados = buscar_catalogo_qdrant(user.id, consulta, limite=DEFAULT_SEARCH_LIMIT)
    productos = []
    for hit in resultados:
        if hasattr(hit, 'payload') and isinstance(hit.payload, dict):
            productos.append(_formatear_producto(hit.payload))
    return jsonify(productos)
