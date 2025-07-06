from flask import Blueprint, request, jsonify
from routes.auth import token_requerido
from services.cart import add_item, remove_item, update_item, clear_cart, get_summary

cart_bp = Blueprint('cart_bp', __name__, url_prefix='/cart')

@cart_bp.route('', methods=['GET'])
@token_requerido
def summary(user):
    return jsonify(get_summary())

@cart_bp.route('/add', methods=['POST'])
@token_requerido
def add(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    add_item(nombre, cantidad)
    return jsonify(get_summary())

@cart_bp.route('/update', methods=['POST'])
@token_requerido
def update(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    cantidad = int(data.get('cantidad', 1))
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    update_item(nombre, cantidad)
    return jsonify(get_summary())

@cart_bp.route('/remove', methods=['POST'])
@token_requerido
def remove(user):
    data = request.get_json() or {}
    nombre = data.get('nombre')
    if not nombre:
        return jsonify({'error': 'nombre requerido'}), 400
    remove_item(nombre)
    return jsonify(get_summary())

@cart_bp.route('/clear', methods=['POST'])
@token_requerido
def clear(user):
    clear_cart()
    return jsonify(get_summary())
