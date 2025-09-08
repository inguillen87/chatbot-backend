from flask import Blueprint, request, jsonify
from extensions import db
from models import User
from utils.auth_helpers import token_requerido

accessibility_bp = Blueprint(
    "accessibility_bp", __name__, url_prefix="/api/accessibility"
)


@accessibility_bp.route("/<int:user_id>", methods=["GET", "PUT", "OPTIONS"])
@token_requerido
def accessibility_preferences(auth_user: User, user_id: int):
    if request.method == "OPTIONS":
        return "", 204

    target_user = User.query.get(user_id)
    if not target_user:
        return jsonify({"error": "User not found"}), 404
    if auth_user.id != target_user.id:
        return jsonify({"error": "Permisos insuficientes"}), 403

    defaults = {"dyslexia": False, "simplified": True}
    if request.method == "GET":
        prefs = target_user.accesibilidad or {}
        result = defaults.copy()
        for key in defaults:
            if key in prefs:
                result[key] = bool(prefs[key])
        return jsonify(result)

    data = request.get_json(silent=True) or {}
    accesibilidad = target_user.accesibilidad or {}
    for key in defaults:
        if key in data:
            accesibilidad[key] = bool(data[key])
    target_user.accesibilidad = accesibilidad
    db.session.commit()
    result = defaults.copy()
    for key in defaults:
        if key in accesibilidad:
            result[key] = bool(accesibilidad[key])
    return jsonify(result)


@accessibility_bp.after_request
def apply_cors(response):
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type,Authorization,X-Anon-Id,Anon-Id"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET,PUT,OPTIONS"
    return response
