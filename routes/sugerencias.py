from flask import Blueprint, request, jsonify
from models import db, Sugerencia, User
from routes.auth import obtener_token
from datetime import datetime

sugerencia_bp = Blueprint("sugerencia", __name__)

@sugerencia_bp.route("/sugerencia", methods=["POST", "OPTIONS"])
def recibir_sugerencia():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    data = request.get_json()
    texto = data.get("texto")
    rubro_id = data.get("rubro_id")
    token = obtener_token()

    if not texto or not rubro_id:
        return jsonify({"error": "Faltan datos requeridos"}), 400

    user = None
    if token:
        user = User.query.filter_by(token=token).first()

    nueva = Sugerencia(
        texto=texto,
        rubro_id=rubro_id,
    )
    if user:
        nueva.user_id = user.id

    db.session.add(nueva)
    db.session.commit()

    return jsonify({"mensaje": "Sugerencia guardada correctamente"}), 201


# ✅ FIX CORS manual
@sugerencia_bp.after_request
def apply_cors(response):
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type,Authorization,Anon-Id,x-entity-token"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response
