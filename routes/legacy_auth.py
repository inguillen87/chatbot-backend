from flask import Blueprint, jsonify
from utils.auth_helpers import token_requerido
from routes.auth import build_profile_payload

legacy_auth_bp = Blueprint('legacy_auth', __name__)

@legacy_auth_bp.route('/me', methods=['GET', 'OPTIONS'])
@legacy_auth_bp.route('/perfil', methods=['GET', 'OPTIONS'])
@legacy_auth_bp.route('/profile', methods=['GET', 'OPTIONS'])
@token_requerido
def get_current_user(user):
    payload = build_profile_payload(user)
    return jsonify({k: v for k, v in payload.items() if v is not None})
