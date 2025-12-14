from flask import Blueprint, jsonify, current_app
from models import Rubro
from services.demo_registry import load_demo_rubros

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
        demo_entries = load_demo_rubros(require_owner=False)
        demo_lookup_by_id = {demo.rubro_id: demo for demo in demo_entries if demo.rubro_id}
        demo_lookup_by_clave = {demo.rubro_clave: demo for demo in demo_entries if demo.rubro_clave}

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

            demo_meta = demo_lookup_by_id.get(rubro.id) or demo_lookup_by_clave.get(
                rubro.clave
            )
            if demo_meta:
                item["demo"] = demo_meta.to_public_dict()

            lista_rubros.append(item)

        matched_demo_keys = {
            demo_meta.key for demo_meta in demo_lookup_by_id.values() if demo_meta
        }
        matched_demo_keys.update(
            demo_lookup_by_clave.get(rubro.clave).key
            for rubro in rubros
            if demo_lookup_by_clave.get(rubro.clave)
        )

        if not lista_rubros:
            for demo in demo_entries:
                lista_rubros.append(
                    {
                        "id": None,
                        "nombre": demo.label,
                        "clave": demo.rubro_clave or demo.key,
                        "descripcion": demo.descripcion,
                        "es_publico": True,
                        "padre_id": None,
                        "demo": demo.to_public_dict(),
                    }
                )
        else:
            for demo in demo_entries:
                if demo.key in matched_demo_keys:
                    continue
                lista_rubros.append(
                    {
                        "id": demo.rubro_id,
                        "nombre": demo.label,
                        "clave": demo.rubro_clave or demo.key,
                        "descripcion": demo.descripcion,
                        "es_publico": True,
                        "padre_id": None,
                        "demo": demo.to_public_dict(),
                    }
                )

        return jsonify(lista_rubros)
    except Exception as e:  # pragma: no cover - log unexpected errors
        current_app.logger.exception(f"Error al obtener la lista de rubros: {e}")
        return jsonify({"error": "Error interno al obtener los rubros."}), 500
