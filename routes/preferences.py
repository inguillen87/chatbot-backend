from flask import Blueprint, request, jsonify
from extensions import db
from models import User
from utils.auth_helpers import token_requerido

preferences_bp = Blueprint('preferences_bp', __name__, url_prefix='/preferences')

@preferences_bp.route('/accessibility', methods=['GET', 'PUT', 'OPTIONS'])
@token_requerido
def accessibility_preferences(user: User):
    if request.method == 'OPTIONS':
        return '', 204
    if request.method == 'GET':
        return jsonify(user.accesibilidad or {})
    data = request.get_json(silent=True) or {}
    accesibilidad = user.accesibilidad or {}
    for key in ('dislexia', 'tts', 'texto_simplificado'):
        if key in data:
            accesibilidad[key] = bool(data[key])
    user.accesibilidad = accesibilidad
    db.session.commit()
    return jsonify(user.accesibilidad)
