from flask import Blueprint, jsonify, current_app
from models import Rubro
from services.demo_registry import demo_rubros_for_rubros

# strict_slashes=False permite acceder tanto a '/rubros' como a '/rubros/'
rubros_bp = Blueprint("rubros", __name__, url_prefix="/rubros")


@rubros_bp.route("/", methods=["GET"], strict_slashes=False)
def get_all_rubros():
    """Return the list of rubros."""
    try:
        rubros = Rubro.query.order_by(Rubro.nombre.asc()).all()
        demo_lookup = demo_rubros_for_rubros(r.id for r in rubros)

        lista_rubros = []
        for rubro in rubros:
            item = {
                "id": rubro.id,
                "nombre": rubro.nombre,
                "clave": rubro.clave,
                "descripcion": rubro.descripcion,
                "es_publico": bool(rubro.es_publico),
                "padre_id": rubro.padre_id,
            }

            demo_meta = demo_lookup.get(rubro.id)
            if demo_meta:
                item["demo"] = demo_meta.to_public_dict()

            lista_rubros.append(item)

        return jsonify(lista_rubros)
    except Exception as e:  # pragma: no cover - log unexpected errors
        current_app.logger.exception(f"Error al obtener la lista de rubros: {e}")
        return jsonify({"error": "Error interno al obtener los rubros."}), 500
