from flask import Blueprint, render_template, current_app, request, jsonify
import os
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from services.ticket_service import servicio_tickets


estadisticas_bp = Blueprint("estadisticas", __name__, url_prefix="/estadisticas")


@estadisticas_bp.route("/mapa_calor")
@token_requerido
@admin_o_empleado_requerido
def mapa_calor(current_user):
    """Renderiza el mapa de calor."""
    maptiler_key = current_app.config.get("MAPTILER_KEY") or os.getenv("VITE_MAPTILER_KEY", "")
    return render_template("estadisticas.html", maptiler_key=maptiler_key)


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
    )
    return jsonify(puntos)
