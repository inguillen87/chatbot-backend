from flask import Blueprint, jsonify, current_app
from models import Rubro

# strict_slashes=False permite acceder tanto a '/rubros' como a '/rubros/'
rubros_bp = Blueprint("rubros", __name__, url_prefix="/rubros")


@rubros_bp.route("/", methods=["GET"], strict_slashes=False)
def get_all_rubros():
    """Return the list of rubros."""
    try:
        rubros = Rubro.query.order_by(Rubro.nombre.asc()).all()
        lista_rubros = [{"id": r.id, "nombre": r.nombre} for r in rubros]
        return jsonify(lista_rubros)
    except Exception as e:  # pragma: no cover - log unexpected errors
        current_app.logger.exception(f"Error al obtener la lista de rubros: {e}")
        return jsonify({"error": "Error interno al obtener los rubros."}), 500
