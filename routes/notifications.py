from flask import Blueprint, jsonify, make_response, request
from models import User
from routes.auth import _add_cors, token_requerido
from utils.auth_helpers import _set_anon_cookie, get_or_create_anon_id

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/notifications', methods=['OPTIONS'])
def notifications_options():
    """Responder a los preflight requests con los encabezados necesarios."""
    anon_id = get_or_create_anon_id()
    resp = make_response("", 200)
    resp = _add_cors(
        resp,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=(
            "Authorization, Content-Type, Origin, Accept, "
            "X-Entity-Token, X-Chat-Session-Id, X-Anon-Id, Anon-Id, "
            "x-anon-id, anon-id, X-Tenant, x-tenant, X-Tenant-Id, x-tenant-id"
        ),
    )
    resp.headers.setdefault("X-Anon-Id", anon_id)
    resp.headers.setdefault("Anon-Id", anon_id)
    return _set_anon_cookie(resp, anon_id)


@notifications_bp.route('/notifications', methods=['GET'])
@token_requerido
def get_notifications(current_user: User):
    """Devuelve notificaciones pendientes del usuario actual (placeholder)."""
    # TODO: hook into real notification logic once available
    return jsonify([])
                                                    