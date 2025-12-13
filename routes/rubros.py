from flask import Blueprint, jsonify, current_app, request
from models import Rubro
from services.demo_registry import demo_rubros_for_rubros

# Define blueprint without prefix here so it can be mounted flexibly in app.py
# (e.g. at /rubros AND /api/rubros)
rubros_bp = Blueprint("rubros", __name__)


@rubros_bp.route("/", methods=["GET"], strict_slashes=False)
def get_all_rubros():
    """Return the list of rubros."""
    try:
        # Filter only public rubros to avoid exposing hidden legacy/test data
        # and to reduce the payload size if many hidden items exist.
        rubros = Rubro.query.filter_by(es_publico=True).order_by(Rubro.nombre.asc()).all()
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

        # Optional: Return hierarchical structure
        if request.args.get("format") == "tree":
            return jsonify(_build_tree(lista_rubros))

        return jsonify(lista_rubros)
    except Exception as e:  # pragma: no cover - log unexpected errors
        current_app.logger.exception(f"Error al obtener la lista de rubros: {e}")
        return jsonify({"error": "Error interno al obtener los rubros."}), 500

def _build_tree(flat_list):
    """Builds a nested tree structure from a flat list of rubros."""
    node_map = {item['id']: {**item, 'children': []} for item in flat_list}
    tree = []

    for item in flat_list:
        node = node_map[item['id']]
        if item['padre_id'] and item['padre_id'] in node_map:
            parent = node_map[item['padre_id']]
            parent['children'].append(node)
        else:
            # Root node (or orphan if parent is not in the list)
            tree.append(node)

    return tree
