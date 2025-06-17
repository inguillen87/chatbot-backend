from flask import Blueprint, request, jsonify
from models import CatalogoItem
from routes.auth import token_requerido
from services.qdrant_search import buscar_catalogo_qdrant, DEFAULT_SEARCH_LIMIT

catalogo_bp = Blueprint('catalogo', __name__, url_prefix='/catalogo')


def _formatear_producto(data: dict) -> dict:
    precio_raw = data.get('precio_float') if data.get('precio_float') is not None else data.get('precio_str')
    if isinstance(precio_raw, str) and not precio_raw.strip():
        precio_raw = None

    return {
        'nombre': data.get('nombre', ''),
        'categoria': data.get('categoria') or data.get('categoria_qdrant', ''),
        'descripcion': data.get('descripcion'),
        'sku': data.get('sku'),
        'presentacion': data.get('unidad') or data.get('presentacion', ''),
        'talles': data.get('talles'),
        'colores': data.get('colores'),
        'precio_unitario': precio_raw,
        'precio_pack': data.get('precio_pack'),
        'stock': data.get('cantidad'),
        'marca': data.get('marca'),
        'imagen_url': data.get('imagen_url'),
    }


@catalogo_bp.route('', methods=['GET'])
@token_requerido
def listar_catalogo(user):
    items = CatalogoItem.query.filter_by(user_id=user.id).all()
    productos = []
    for item in items:
        productos.append(
            _formatear_producto({
                'nombre': item.nombre,
                'categoria': item.categoria,
                'descripcion': item.descripcion,
                'sku': item.sku,
                'unidad': item.unidad,
                'precio_str': item.precio,
                'cantidad': item.cantidad,
                'marca': item.marca,
            })
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
