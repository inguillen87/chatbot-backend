from flask import Blueprint, jsonify
from routes.auth import token_requerido
from utils.permissions import require_role
from services.categorias_municipio import TODAS_LAS_CATEGORIAS_UNICAS

categorias_bp = Blueprint('categorias', __name__, url_prefix='/categorias')

@categorias_bp.route('', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def obtener_categorias(current_user):
    """Devuelve la lista de categorías de tickets disponibles.

    Para compatibilidad con versiones anteriores, se devuelve tanto la clave
    en español (`categorias`) como su equivalente en inglés (`categories`).
    """
    return jsonify({
        "categorias": TODAS_LAS_CATEGORIAS_UNICAS,
        "categories": TODAS_LAS_CATEGORIAS_UNICAS,
    })
