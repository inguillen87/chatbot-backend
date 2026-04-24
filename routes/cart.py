from flask import Blueprint, request, jsonify, session
from routes.auth import token_requerido
from services.cart import add_item, remove_item, update_item, clear_cart, get_summary

cart_bp = Blueprint('cart_bp', __name__, url_prefix='/cart')


def _get_session_cart_data():
    """Ensure there is a dictionary in session to hold cart data."""
    pyme_carts_data = session.get('carritos_pymes')
    if not isinstance(pyme_carts_data, dict):
        pyme_carts_data = {}
        session['carritos_pymes'] = pyme_carts_data
    return pyme_carts_data


def _persist_session_cart_data(pyme_carts_data):
    """Persist cart data changes back to the session."""
    session['carritos_pymes'] = pyme_carts_data
    session.modified = True

@cart_bp.route('', methods=['GET'])
@token_requerido
def summary(user):
    pyme_carts_data = _get_session_cart_data()
    return jsonify(get_summary(pyme_carts_data))

@cart_bp.route('/add', methods=['POST'])
@token_requerido
def add(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    add_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))

@cart_bp.route('/update', methods=['POST'])
@token_requerido
def update(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    update_item(pyme_carts_data, nombre, cantidad)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))

@cart_bp.route('/remove', methods=['POST'])
@token_requerido
def remove(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    pyme_carts_data = _get_session_cart_data()
    remove_item(pyme_carts_data, nombre)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))

@cart_bp.route('/clear', methods=['POST'])
@token_requerido
def clear(user):
    pyme_carts_data = _get_session_cart_data()
    clear_cart(pyme_carts_data)
    _persist_session_cart_data(pyme_carts_data)
    return jsonify(get_summary(pyme_carts_data))
