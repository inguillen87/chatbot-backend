from flask import Blueprint, request, jsonify
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from services.ticket_service import servicio_tickets
from models import User, MunicipioTicket, PymeTicket, db


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
        agrupar=args.get("agrupar", default="true").lower() != "false",
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


@estadisticas_bp.route("/tickets", methods=["OPTIONS"])
def tickets_options():
    """Preflight CORS para /estadisticas/tickets."""
    return "", 200


@estadisticas_bp.route("/tickets", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def estadisticas_tickets(current_user):
    """Devuelve datos de tickets para gráficas y mapas de calor.

    Responde con un objeto JSON que contiene la clave `heatmap` con los
    puntos agregados para el mapa de calor. Si no se especifica el
    `municipio_id` o `rubro_id`, se utilizan los del `current_user`.
    """
    args = request.args
    tipo = args.get("tipo", "municipio")

    municipio_id = args.get("municipio_id", type=int)
    rubro_id = args.get("rubro_id", type=int)

    if tipo == "municipio" and municipio_id is None:
        municipio_id = getattr(current_user, "municipio_id", None)
    if tipo == "pyme" and rubro_id is None:
        rubro_id = getattr(current_user, "rubro_id", None)

    agrupar = args.get("agrupar", default="true").lower() != "false"
    puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=tipo,
        municipio_id=municipio_id,
        rubro_id=rubro_id,
        fecha_inicio=args.get("fecha_inicio"),
        fecha_fin=args.get("fecha_fin"),
        categoria=args.get("categoria"),
        estado=args.get("estado"),
        satisfactorio=args.get(
            "satisfactorio", type=lambda v: str(v).lower() == "true"
        ),
        agrupar=agrupar,
    )
    key = "heatmap" if agrupar else "puntos"
    return jsonify({key: puntos})


@estadisticas_bp.route("/categorias", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def estadisticas_categorias(current_user):
    """Lista las categorías existentes en los tickets."""
    tipo = request.args.get("tipo", "municipio")
    municipio_id = request.args.get("municipio_id", type=int)
    rubro_id = request.args.get("rubro_id", type=int)

    if tipo == "municipio":
        query = db.session.query(MunicipioTicket.categoria).filter(MunicipioTicket.categoria.isnot(None))
        if municipio_id is None:
            municipio_id = getattr(current_user, "municipio_id", None)
        if municipio_id is not None:
            query = query.filter(MunicipioTicket.municipio_id == municipio_id)
    else:
        query = db.session.query(PymeTicket.categoria).filter(PymeTicket.categoria.isnot(None))
        if rubro_id is None:
            rubro_id = getattr(current_user, "rubro_id", None)
        if rubro_id is not None:
            query = query.filter(PymeTicket.rubro_id == rubro_id)

    categorias = sorted({c[0] for c in query.distinct()})
    return jsonify(categorias)
