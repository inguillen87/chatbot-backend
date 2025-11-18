from flask import Blueprint, request, jsonify, session
from routes.auth import token_requerido
from services.cart import add_item, remove_item, update_item, clear_cart, get_summary

carrito_bp = Blueprint('carrito_bp', __name__, url_prefix='/carrito')


def _get_session_cart_data():
    pyme_carts_data = session.get('carritos_pymes')
    if not isinstance(pyme_carts_data, dict):
        pyme_carts_data = {}
        session['carritos_pymes'] = pyme_carts_data
    return pyme_carts_data


def _persist_session_cart_data(pyme_carts_data):
    session['carritos_pymes'] = pyme_carts_data
    session.modified = True

@carrito_bp.route('', methods=['GET'])
@carrito_bp.route('/', methods=['GET'])
@token_requerido
def obtener_carrito(user):
    """Alias de /carrito/resumen para compatibilidad."""
    pyme_carts_data = _get_session_cart_data()
    return jsonify(get_summary(pyme_carts_data))


@carrito_bp.route('/agregar', methods=['POST'])
@token_requerido
def agregar(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    add_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))


@carrito_bp.route('/actualizar', methods=['POST'])
@token_requerido
def actualizar(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    update_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))


@carrito_bp.route('/eliminar', methods=['POST'])
@token_requerido
def eliminar(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    remove_item(pyme_carts_data, nombre)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))


@carrito_bp.route('/vaciar', methods=['POST'])
@token_requerido
def vaciar(user):
    pyme_carts_data = _get_session_cart_data()
    clear_cart(pyme_carts_data)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))


@carrito_bp.route('/resumen', methods=['GET'])
@token_requerido
def resumen(user):
    pyme_carts_data = _get_session_cart_data()
    return jsonify(get_summary(pyme_carts_data))
