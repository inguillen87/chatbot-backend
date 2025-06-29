from flask import Blueprint, jsonify
from models import User
from routes.auth import token_requerido

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/notifications', methods=['GET'])
@token_requerido
def get_notifications(current_user: User):
    """Devuelve notificaciones pendientes del usuario actual (placeholder)."""
    # TODO: hook into real notification logic once available
    return jsonify([])
