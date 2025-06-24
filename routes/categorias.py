from flask import Blueprint, jsonify
from routes.auth import token_requerido
from utils.permissions import require_role
from services.municipios import TODAS_LAS_CATEGORIAS_UNICAS

categorias_bp = Blueprint('categorias', __name__, url_prefix='/categorias')

@categorias_bp.route('', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def obtener_categorias(current_user):
    """Devuelve la lista de categorías de tickets disponibles."""
    return jsonify(TODAS_LAS_CATEGORIAS_UNICAS)
