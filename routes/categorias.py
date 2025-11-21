from flask import Blueprint, jsonify, request
from routes.auth import token_requerido
from utils.permissions import require_role
# La lista de categorías disponible para asignar a los empleados y filtrar
# tickets proviene de ``services.categorias_municipio``.  Anteriormente
# se utilizaba ``TODAS_LAS_CATEGORIAS_UNICAS`` derivado de un mapa de
# palabras clave, lo que dejaba fuera categorías como ``Sugerencia`` u
# ``Otro Motivo``.  Para exponer todas las categorías relevantes al
# frontend de administración utilizamos directamente ``CATEGORIAS_RECLAMO``
# y capitalizamos cada entrada para una mejor presentación.
from services.categorias_municipio import CATEGORIAS_RECLAMO

categorias_bp = Blueprint('categorias', __name__, url_prefix='/categorias')

@categorias_bp.route('', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def obtener_categorias(current_user):
    """Devuelve la lista de categorías de tickets disponibles.

    Para compatibilidad con versiones anteriores, se devuelve tanto la clave
    en español (`categorias`) como su equivalente en inglés (`categories`).
    """
    categorias = [c.title() for c in CATEGORIAS_RECLAMO]
    search_term = (request.args.get("q") or "").strip().lower()
    if search_term:
        categorias = [c for c in categorias if search_term in c.lower()]
    return jsonify({
        "categorias": categorias,
        "categories": categorias,
    })
