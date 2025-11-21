from flask import Blueprint, jsonify, make_response, request
from models import User
from routes.auth import token_requerido
from utils.auth_helpers import _set_anon_cookie, get_or_create_anon_id

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/notifications', methods=['OPTIONS'])
def notifications_options():
    """Responder a los preflight requests con los encabezados necesarios."""
    anon_id = get_or_create_anon_id()
    resp = make_response("", 200)
    origin = request.headers.get("Origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    else:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Origin, Accept, "
        "X-Entity-Token, X-Chat-Session-Id, X-Anon-Id, Anon-Id, "
        "x-anon-id, anon-id"
    )
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers.setdefault("X-Anon-Id", anon_id)
    resp.headers.setdefault("Anon-Id", anon_id)
    return _set_anon_cookie(resp, anon_id)


@notifications_bp.route('/notifications', methods=['GET'])
@token_requerido
def get_notifications(current_user: User):
    """Devuelve notificaciones pendientes del usuario actual (placeholder)."""
    # TODO: hook into real notification logic once available
    return jsonify([])
                                                    