from flask import Blueprint, request, jsonify, session
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


def _carrito_summary_response():
    pyme_carts_data = _get_session_cart_data()
    return jsonify(get_summary(pyme_carts_data))


@carrito_bp.route('', methods=['GET', 'POST'])
@carrito_bp.route('/', methods=['GET', 'POST'])
def carrito_root():
    """Permite consultar el carrito (GET) o agregar items (POST) desde la raíz."""
    if request.method == 'GET':
        return _carrito_summary_response()
    return agregar()


@carrito_bp.route('/agregar', methods=['POST'])
def agregar():
    data = request.get_json(silent=True) or {}
    nombre = data.get('nombre')
    try:
        cantidad = int(data.get('cantidad', 1))
    except (TypeError, ValueError):
        cantidad = 1
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    add_item(pyme_carts_data, nombre, max(1, cantidad))
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/actualizar', methods=['POST'])
def actualizar():
    data = request.get_json(silent=True) or {}
    nombre = data.get('nombre')
    try:
        cantidad = int(data.get('cantidad', 1))
    except (TypeError, ValueError):
        cantidad = 1
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    update_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/eliminar', methods=['POST'])
def eliminar():
    data = request.get_json(silent=True) or {}
    nombre = data.get('nombre')
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    remove_item(pyme_carts_data, nombre)
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/vaciar', methods=['POST'])
def vaciar():
    pyme_carts_data = _get_session_cart_data()
    clear_cart(pyme_carts_data)
    _persist_session_cart_data(pyme_carts_data)
    return _carrito_summary_response()


@carrito_bp.route('/resumen', methods=['GET'])
def resumen():
    return _carrito_summary_response()
