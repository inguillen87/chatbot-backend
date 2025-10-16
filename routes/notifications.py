from flask import Blueprint, jsonify, make_response, request
from models import User
from routes.auth import token_requerido

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/notifications', methods=['OPTIONS'])
def notifications_options():
    """Preflight CORS handler for /notifications."""
    response = make_response("", 200)
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Origin, Accept, "
        "X-Entity-Token, X-Chat-Session-Id, X-Anon-Id, Anon-Id"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


@notifications_bp.route('/notifications', methods=['GET'])
@token_requerido
def get_notifications(current_user: User):
    """Devuelve notificaciones pendientes del usuario actual (placeholder)."""
    # TODO: hook into real notification logic once available
    return jsonify([])
                                                    