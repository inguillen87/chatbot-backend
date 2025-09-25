from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g

from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from utils.time_utils import get_local_now
from services.ticket_service import servicio_tickets
from services.municipal_stats import build_stats_for_municipio, StatsFilters
from models import User


estadisticas_bp = Blueprint("estadisticas", __name__, url_prefix="/estadisticas")


def _resolve_municipio_id(user):
    """Return the municipio identifier associated with the current request."""

    municipio_id = getattr(user, "municipio_id", None)
    if municipio_id is not None:
        return municipio_id

    owner_user = getattr(g, "owner_user", None)
    if owner_user is not None:
        owner_municipio_id = getattr(owner_user, "municipio_id", None)
        if owner_municipio_id is not None:
            return owner_municipio_id
        if getattr(owner_user, "tipo_chat", None) == "municipio":
            owner_id = getattr(owner_user, "id", None)
            if owner_id is not None:
                return owner_id

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id is not None:
        return empresa_id

    if getattr(user, "tipo_chat", None) == "municipio":
        fallback_id = getattr(user, "id", None)
        if fallback_id is not None:
            return fallback_id

    return None


def _parse_multi_value_param(args, key: str) -> list[str]:
    """Devuelve los valores únicos enviados para ``key`` en el query string."""

    valores: list[str] = []

    valores.extend([valor.strip() for valor in args.getlist(key) if valor])
    valores.extend(
        [valor.strip() for valor in args.getlist(f"{key}[]") if valor]
    )

    if not valores:
        raw = args.get(key)
        if raw:
            valores.extend(
                [parte.strip() for parte in raw.split(",") if parte.strip()]
            )

    if not valores:
        return []

    vistos: set[str] = set()
    resultado: list[str] = []
    for valor in valores:
        if valor and valor not in vistos:
            resultado.append(valor)
            vistos.add(valor)

    return resultado


def _parse_estado_params(args) -> list[str] | None:
    """Normaliza los parámetros ``estado`` del query string."""

    estados = _parse_multi_value_param(args, "estado")
    return estados or None


def _parse_int_params(args, key: str) -> tuple[int, ...]:
    """Convierte parámetros repetibles a tuplas de enteros."""

    valores_crudos = _parse_multi_value_param(args, key)
    if not valores_crudos:
        return ()

    enteros: list[int] = []
    vistos: set[int] = set()
    for raw in valores_crudos:
        try:
            numero = int(raw)
        except (TypeError, ValueError):
            continue
        if numero not in vistos:
            vistos.add(numero)
            enteros.append(numero)

    return tuple(enteros)


def _parse_iso_datetime(value: str | None, *, is_end: bool = False) -> datetime | None:
    """Parsea fechas ISO añadiendo zona horaria local y normalizando fin de rango."""

    if not value:
        return None

    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    tzinfo = get_local_now().tzinfo
    if parsed.tzinfo is None and tzinfo is not None:
        parsed = parsed.replace(tzinfo=tzinfo)

    if is_end and "T" not in raw:
        parsed = parsed + timedelta(days=1)

    return parsed


def _build_stats_filters(args, estados: list[str] | None) -> StatsFilters | None:
    """Construye ``StatsFilters`` reutilizando los parámetros del request."""

    categorias = _parse_multi_value_param(args, "categoria")
    distritos = _parse_multi_value_param(args, "distrito")
    canales = _parse_multi_value_param(args, "canal")
    agentes = _parse_int_params(args, "agente_id")
    if not agentes:
        agentes = _parse_int_params(args, "agente")

    filtros = StatsFilters(
        fecha_inicio=_parse_iso_datetime(args.get("fecha_inicio")),
        fecha_fin=_parse_iso_datetime(args.get("fecha_fin"), is_end=True),
        estados=tuple(estados) if estados else None,
        categorias=tuple(categorias) if categorias else None,
        distritos=tuple(distritos) if distritos else None,
        canales=tuple(canales) if canales else None,
        agentes=agentes or None,
    )

    if filtros.is_empty():
        return None

    return filtros


def _serialize_filters(filters: StatsFilters | None) -> dict:
    """Convierte ``StatsFilters`` a un diccionario serializable."""

    if not filters:
        return {}

    def _maybe_iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "fecha_inicio": _maybe_iso(filters.fecha_inicio),
        "fecha_fin": _maybe_iso(filters.fecha_fin),
        "estados": list(filters.estados) if filters.estados else [],
        "categorias": list(filters.categorias) if filters.categorias else [],
        "distritos": list(filters.distritos) if filters.distritos else [],
        "canales": list(filters.canales) if filters.canales else [],
        "agentes": list(filters.agentes) if filters.agentes else [],
    }


def _build_summary_cards(summary: dict) -> list[dict]:
    """Devuelve un conjunto fijo de tarjetas KPI para el tablero municipal."""

    abiertos = int(summary.get("abiertos", 0) or 0)
    en_proceso = int(summary.get("en_proceso", 0) or 0)
    resueltos = int(summary.get("resueltos", 0) or 0)

    return [
        {"label": "Reclamos Abiertos", "value": abiertos},
        {"label": "Reclamos en Proceso", "value": en_proceso},
        {"label": "Reclamos Resueltos", "value": resueltos},
    ]


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

    distritos = _parse_multi_value_param(args, "distrito")
    distrito = distritos[0] if distritos else None

    municipio_id = args.get("municipio_id", type=int)
    if municipio_id is None and args.get("tipo_ticket", "municipio") == "municipio":
        municipio_id = _resolve_municipio_id(current_user)

    categorias = _parse_multi_value_param(args, "categoria")

    puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=args.get("tipo_ticket", "municipio"),
        municipio_id=municipio_id,
        rubro_id=args.get("rubro_id", type=int),
        fecha_inicio=args.get("fecha_inicio"),
        fecha_fin=args.get("fecha_fin"),
        categoria=categorias[0] if categorias else None,
        distrito=distrito,
        estado=estado_param,
        satisfactorio=args.get("satisfactorio", type=lambda v: str(v).lower() == "true"),
    )

    payload: dict[str, object] = {"heatmap": puntos}

    if args.get("tipo_ticket", "municipio") == "municipio":
        stats_filters = _build_stats_filters(args, estados)
        if stats_filters:
            stats = build_stats_for_municipio(municipio_id, filters=stats_filters)
        else:
            stats = build_stats_for_municipio(municipio_id)

        resumen = dict(stats.get("resumen", {})) if isinstance(stats, dict) else {}
        payload["stats"] = stats
        payload["summary"] = resumen
        payload["cards"] = _build_summary_cards(resumen)
        payload["filters"] = _serialize_filters(stats_filters)

    return jsonify(payload)


@estadisticas_bp.route("/usuarios/ubicaciones", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def get_user_locations(current_user):
    """Devuelve las ubicaciones (lat, lng) de usuarios del mismo municipio."""
    municipio_id = _resolve_municipio_id(current_user)
    if municipio_id is None:
        return jsonify([])

    users = (
        User.query.filter(
            User.municipio_id == municipio_id,
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
        municipio_id = _resolve_municipio_id(current_user)
    if tipo == "pyme" and rubro_id is None:
        rubro_id = getattr(current_user, "rubro_id", None)

    categoria_values = _parse_multi_value_param(args, "categoria")
    categoria = categoria_values[0] if categoria_values else None

    puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=tipo,
        municipio_id=municipio_id,
        rubro_id=rubro_id,
        fecha_inicio=args.get("fecha_inicio"),
        fecha_fin=args.get("fecha_fin"),
        categoria=categoria,
        distrito=distrito,
        estado=estado_param,
        satisfactorio=args.get(
            "satisfactorio", type=lambda v: str(v).lower() == "true"
        ),
    )

    respuesta: dict[str, object] = {"heatmap": puntos}

    if tipo == "municipio":
        stats_filters = _build_stats_filters(args, estados)
        if stats_filters:
            stats = build_stats_for_municipio(municipio_id, filters=stats_filters)
        else:
            stats = build_stats_for_municipio(municipio_id)

        resumen = dict(stats.get("resumen", {})) if isinstance(stats, dict) else {}
        respuesta["stats"] = stats
        respuesta["summary"] = resumen
        respuesta["cards"] = _build_summary_cards(resumen)
        respuesta["filters"] = _serialize_filters(stats_filters)

    return jsonify(respuesta)
