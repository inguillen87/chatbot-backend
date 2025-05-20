from flask import Blueprint, jsonify
from models import Rubro

rubros_bp = Blueprint("rubros", __name__)

@rubros_bp.route("/rubros", methods=["GET"])
def get_rubros():
    rubros = Rubro.query.all()
    return jsonify({
        "rubros": [{"id": r.id, "nombre": r.nombre} for r in rubros]
    })
