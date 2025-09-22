from flask import Blueprint, request, jsonify
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from services.ticket_service import servicio_tickets
from models import User


estadisticas_bp = Blueprint("estadisticas", __name__, url_prefix="/estadisticas")


def _parse_estado_params(args) -> list[str] | None:
    """Normaliza los parámetros ``estado`` del query string."""

    estados: list[str] = []

    # Permitir múltiples valores ?estado=foo&estado=bar
    estados.extend([valor.strip() for valor in args.getlist("estado") if valor])

    # Compatibilidad con clientes que envían estado[]=valor
    if not estados:
        estados.extend(
            [valor.strip() for valor in args.getlist("estado[]") if valor]
        )

    # Compatibilidad hacia atrás con un único parámetro comma-separated
    if not estados:
        estado_unico = args.get("estado")
        if estado_unico:
            estados.extend(
                [parte.strip() for parte in estado_unico.split(",") if parte.strip()]
            )

    # Quitar duplicados preservando el orden
    if estados:
        vistos: set[str] = set()
        estados_unicos = []
        for estado in estados:
            if estado and estado not in vistos:
                estados_unicos.append(estado)
                vistos.add(estado)
        return estados_unicos or None

    return None


@estadisticas_bp.route("/mapa_calor/datos", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def mapa_calor_datos(current_user):
    """Devuelve los puntos para el mapa de calor en formato JSON."""
    args = request.args
    estados = _parse_estado_params(args)
    estado_param = None
    if estados:
        estado_param = estados if len(estados) > 1 else estados[0]

    distrito = args.get("distrito", type=str)
    if distrito:
        distrito = distrito.strip() or None

    puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=args.get("tipo_ticket", "municipio"),
        municipio_id=args.get("municipio_id", type=int),
        rubro_id=args.get("rubro_id", type=int),
        fecha_inicio=args.get("fecha_inicio"),
        fecha_fin=args.get("fecha_fin"),
        categoria=args.get("categoria"),
        distrito=distrito,
        estado=estado_param,
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

    estados = _parse_estado_params(args)
    estado_param = None
    if estados:
        estado_param = estados if len(estados) > 1 else estados[0]

    distrito = args.get("distrito", type=str)
    if distrito:
        distrito = distrito.strip() or None

    if tipo == "municipio" and municipio_id is None:
        municipio_id = getattr(current_user, "municipio_id", None)
    if tipo == "pyme" and rubro_id is None:
        rubro_id = getattr(current_user, "rubro_id", None)

    puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=tipo,
        municipio_id=municipio_id,
        rubro_id=rubro_id,
        fecha_inicio=args.get("fecha_inicio"),
        fecha_fin=args.get("fecha_fin"),
        categoria=args.get("categoria"),
        distrito=distrito,
        estado=estado_param,
        satisfactorio=args.get(
            "satisfactorio", type=lambda v: str(v).lower() == "true"
        ),
    )

    return jsonify({"heatmap": puntos})
