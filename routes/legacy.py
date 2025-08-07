from flask import Blueprint, request
from .auth import login, get_current_user, actualizar_me, token_requerido
from .ticket import get_tickets_del_usuario

legacy_bp = Blueprint('legacy', __name__)

@legacy_bp.route('/login', methods=['POST'])
def legacy_login():
    return login()

@legacy_bp.route('/perfil', methods=['GET', 'PUT'])
@token_requerido
def legacy_perfil(user):
    if request.method == 'GET':
        return get_current_user(user)
    elif request.method == 'PUT':
        return actualizar_me(user)

@legacy_bp.route('/tickets', methods=['GET'])
@token_requerido
def legacy_tickets(user):
    return get_tickets_del_usuario(user)
