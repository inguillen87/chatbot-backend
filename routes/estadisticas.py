from flask import Blueprint, request, jsonify
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from services.ticket_service import servicio_tickets
from models import User


estadisticas_bp = Blueprint("estadisticas", __name__, url_prefix="/estadisticas")


@estadisticas_bp.route("/mapa_calor/datos", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def mapa_calor_datos(current_user):
    """Devuelve los puntos para el mapa de calor en formato JSON."""
    args = request.args
    puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=args.get("tipo_ticket", "municipio"),
        municipio_id=args.get("municipio_id", type=int),
        rubro_id=args.get("rubro_id", type=int),
        fecha_inicio=args.get("fecha_inicio"),
        fecha_fin=args.get("fecha_fin"),
        categoria=args.get("categoria"),
        estado=args.get("estado"),
        satisfactorio=args.get("satisfactorio", type=lambda v: str(v).lower() == "true"),
    )
    return jsonify(puntos)


@estadisticas_bp.route("/usuarios/ubicaciones", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def get_user_locations(current_user):
    """Devuelve las ubicaciones (lat, lng) de usuarios del mismo municipio."""
    users = (
        User.query.filter(
            User.municipio_id == current_user.municipio_id,
            User.latitud.isnot(None),
            User.longitud.isnot(None),
        ).all()
    )
    locations = [{"lat": u.latitud, "lng": u.longitud} for u in users]
    return jsonify(locations)
