from flask import Blueprint, jsonify, request
from models import User, Conversacion, Reaccion
from extensions import db
from routes.auth import token_requerido

reacciones_bp = Blueprint("reacciones", __name__, url_prefix="/reacciones")


@reacciones_bp.route("", methods=["POST"])
@token_requerido
def agregar_reaccion(current_user: User):
    data = request.get_json(silent=True) or {}
    conv_id = data.get("conversacion_id")
    emoji = data.get("emoji")
    if not conv_id or not emoji:
        return jsonify({"error": "Datos incompletos"}), 400
    conv = Conversacion.query.get(conv_id)
    if not conv:
        return jsonify({"error": "Conversacion inexistente"}), 404
    reaccion = Reaccion(conversacion_id=conv_id, user_id=current_user.id, emoji=emoji)
    db.session.add(reaccion)
    db.session.commit()
    return jsonify({"id": reaccion.id}), 201


@reacciones_bp.route("/<int:conv_id>", methods=["GET"])
@token_requerido
def obtener_reacciones(current_user: User, conv_id: int):
    conv = Conversacion.query.get(conv_id)
    if not conv:
        return jsonify({"error": "Conversacion inexistente"}), 404
    counts: dict[str, int] = {}
    for r in conv.reacciones:
        counts[r.emoji] = counts.get(r.emoji, 0) + 1
    return jsonify(counts)
