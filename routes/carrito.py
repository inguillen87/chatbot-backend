from flask import Blueprint, request, jsonify, current_app
from routes.auth import token_requerido
from services.cart import (
    add_item_to_cart, 
    remove_item_from_cart, 
    update_item_quantity_in_cart, 
    clear_pyme_cart, 
    get_cart_summary
)
from models import CatalogoItem, User 
from services.common_utils import parse_precio_flexible

carrito_bp = Blueprint('carrito_bp', __name__, url_prefix='/carrito')


@carrito_bp.route('', methods=['GET'])
@carrito_bp.route('/', methods=['GET'])
@token_requerido
def obtener_carrito(user):
    """Alias de /carrito/resumen para compatibilidad."""
    return jsonify(get_summary())


@carrito_bp.route('/agregar', methods=['POST'])
@token_requerido
def agregar(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    add_item(nombre, cantidad)
    return jsonify(get_summary())


@carrito_bp.route('/actualizar', methods=['POST'])
@token_requerido
def actualizar(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    update_item(nombre, cantidad)
    return jsonify(get_summary())


@carrito_bp.route('/eliminar', methods=['POST'])
@token_requerido
def eliminar(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    remove_item(nombre)
    return jsonify(get_summary())


@carrito_bp.route('/vaciar', methods=['POST'])
@token_requerido
def vaciar(user):
    clear_cart()
    return jsonify(get_summary())


@carrito_bp.route('/resumen', methods=['GET'])
@token_requerido
def resumen(user):
    return jsonify(get_summary())
