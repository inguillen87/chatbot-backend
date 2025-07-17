# En el archivo: routes/rubros.py (Versión Corregida y Final)

from flask import Blueprint, jsonify, current_app
from models import Rubro

# 1. Se define el prefijo de la URL en el Blueprint.
# Esto es más limpio y evita conflictos.
rubros_bp = Blueprint('rubros', __name__)

# Acepta tanto '/rubros' como '/rubros/' para evitar redirecciones
@rubros_bp.route('/rubros', methods=['GET'], strict_slashes=False)
def get_all_rubros():
    """
    Endpoint para obtener la lista de todos los rubros.
    URL final: /rubros/
    """
    try:
        rubros = Rubro.query.order_by(Rubro.nombre.asc()).all()
        lista_rubros = [{"id": rubro.id, "nombre": rubro.nombre} for rubro in rubros]
        return jsonify(lista_rubros)
    except Exception as e:
        # Usamos current_app para acceder al logger configurado en app.py
        current_app.logger.exception(f"Error al obtener la lista de rubros: {e}")
        return jsonify({"error": "Error interno al obtener los rubros."}), 500
