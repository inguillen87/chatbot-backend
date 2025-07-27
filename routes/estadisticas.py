from flask import Blueprint, jsonify, render_template
from sqlalchemy import text
from routes.auth import token_requerido, admin_o_empleado_requerido
from models import db, User
from services.logic import es_rubro_publico
from datetime import datetime, timedelta

estadisticas_bp = Blueprint('estadisticas', __name__, url_prefix='/estadisticas')

@estadisticas_bp.route('/mapa_calor')
@token_requerido
@admin_o_empleado_requerido
def mapa_calor(current_user):
    """Renderiza el mapa de calor."""
    return render_template('estadisticas.html')

@estadisticas_bp.route('/clientes/ubicaciones', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_user_locations(current_user):
    """Devuelve la ubicación de los usuarios para el mapa de calor."""
    if es_rubro_publico(getattr(current_user, "rubro", None)):
        users = User.query.filter(User.municipio_id == current_user.municipio_id, User.latitud.isnot(None), User.longitud.isnot(None)).all()
    else:
        users = User.query.filter(User.empresa_id == current_user.id, User.latitud.isnot(None), User.longitud.isnot(None)).all()

    locations = [{"lat": user.latitud, "lng": user.longitud} for user in users]
    return jsonify(locations)
