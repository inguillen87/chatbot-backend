from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g

from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from utils.time_utils import get_local_now
from services.ticket_service import servicio_tickets
from services.municipal_stats import build_stats_for_municipio, StatsFilters
from services.metricas_service import MetricasService
from models import User
from services.demo_geo import generate_demo_points
from utils.heatmap import (
    build_feature_collection,
    build_google_heatmap,
    enrich_heatmap_points,
)


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


def _parse_bool_param(args, key: str) -> bool | None:
    """Convierte parámetros booleanos del query string."""

    raw = args.get(key)
    if raw is None:
        return None

    texto = str(raw).strip().lower()
    if texto in {"1", "true", "t", "yes", "si", "sí"}:
        return True
    if texto in {"0", "false", "f", "no"}:
        return False

    return None


def _demo_heatmap(scope: str) -> list[dict]:
    demo_points = generate_demo_points(scope=scope, count=90 if scope == "municipio" else 70)
    heatmap: list[dict] = []
    for point in demo_points:
        weight = point.get("count")
        if weight is None:
            total = point.get("total", 0)
            weight = max(1, int(round(total / 2500))) if total else 1
        heatmap.append(
            {
                "lat": point["lat"],
                "lng": point["lon"],
                "location": {"lat": point["lat"], "lng": point["lon"]},
                "weight": weight,
                "categoria": point.get("categoria"),
                "estado": point.get("estado"),
                "barrio": point.get("barrio"),
                "fuente": "demo",
            }
        )
    enrich_heatmap_points(
        heatmap,
        property_keys=("categoria", "estado", "barrio", "fuente"),
    )
    return heatmap


def _augment_heatmap_payload(payload: dict[str, object], *, key: str = "heatmap") -> None:
    """Attach shared heatmap representations used by MapLibre and Google Maps."""

    points = payload.get(key)
    if not isinstance(points, list) or not points:
        return

    enrich_heatmap_points(
        points,
        property_keys=("categoria", "estado", "barrio", "fuente", "canal", "total"),
    )

    feature_collection = build_feature_collection(points)
    if feature_collection:
        payload[f"{key}_geojson"] = feature_collection

    google_points = build_google_heatmap(points)
    if google_points:
        payload[f"{key}_google"] = google_points


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


def _normalize_dashboard_tipo(raw_tipo: str | None) -> str:
    """Normaliza el parámetro ``tipo`` para el dashboard unificado."""

    if not raw_tipo:
        return "municipio"

    tipo = raw_tipo.strip().lower()
    if tipo not in {"municipio", "pyme"}:
        return "municipio"

    return tipo


def _clean_text_param(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned or None


def _build_applied_filters(
    *,
    estados: list[str] | None,
    categorias: list[str],
    distrito: str | None,
    fecha_inicio: str | None,
    fecha_fin: str | None,
    satisfactorio: bool | None,
    municipio_id: int | None,
    rubro_id: int | None,
) -> dict:
    """Construye un diccionario serializable con los filtros aplicados."""

    return {
        "estados": estados or [],
        "categorias": categorias,
        "distrito": distrito,
        "fecha_inicio": fecha_inicio,
        "fecha_fin": fecha_fin,
        "satisfactorio": satisfactorio,
        "municipio_id": municipio_id,
        "rubro_id": rubro_id,
    }


@estadisticas_bp.route("/dashboard", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def estadisticas_dashboard(current_user):
    """Fusiona estadísticas municipales/pyme en un único payload para dashboards modernos."""

    args = request.args
    tipo = _normalize_dashboard_tipo(args.get("tipo", "municipio"))

    estados = _parse_estado_params(args)
    estado_param = None
    if estados:
        estado_param = estados if len(estados) > 1 else estados[0]

    categorias = _parse_multi_value_param(args, "categoria")

    distrito = _clean_text_param(args.get("distrito"))
    fecha_inicio = args.get("fecha_inicio")
    fecha_fin = args.get("fecha_fin")
    satisfactorio = _parse_bool_param(args, "satisfactorio")

    municipio_id = args.get("municipio_id", type=int)
    rubro_id = args.get("rubro_id", type=int)

    if tipo == "municipio" and municipio_id is None:
        municipio_id = _resolve_municipio_id(current_user)
    if tipo == "pyme" and rubro_id is None:
        rubro_id = getattr(current_user, "rubro_id", None)

    heatmap = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=tipo,
        municipio_id=municipio_id,
        rubro_id=rubro_id,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        categoria=categorias or None,
        distrito=distrito,
        estado=estado_param,
        satisfactorio=satisfactorio,
    )
    if not heatmap:
        heatmap = _demo_heatmap(tipo)

    metadata = {
        "municipio_id": municipio_id,
        "rubro_id": rubro_id,
        "last_updated": get_local_now().isoformat(),
    }

    payload: dict[str, object] = {
        "tipo": tipo,
        "heatmap": heatmap,
        "metadata": metadata,
        "applied_filters": _build_applied_filters(
            estados=estados,
            categorias=categorias,
            distrito=distrito,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            satisfactorio=satisfactorio,
            municipio_id=municipio_id,
            rubro_id=rubro_id,
        ),
    }

    _augment_heatmap_payload(payload)

    if tipo == "municipio":
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
    else:
        pyme_id = args.get("pyme_id", type=int)
        if pyme_id is None:
            pyme_id = getattr(current_user, "pyme_id", None) or getattr(current_user, "id", None)

        metadata["pyme_id"] = pyme_id

        resumen_pyme: dict[str, object] = {}
        cards_pyme: list[dict[str, object]] = []
        stats_pyme: dict[str, object] = {}

        if pyme_id:
            metricas = MetricasService(pyme_id)
            total_ventas = metricas.get_total_ingresos()
            total_pedidos = metricas.get_total_pedidos()
            clientes_unicos = metricas.get_new_customers()
            tasa_conversion = metricas.get_conversion_rate()

            resumen_pyme = {
                "total_ventas": total_ventas,
                "total_pedidos": total_pedidos,
                "clientes_unicos": clientes_unicos,
                "tasa_conversion": tasa_conversion,
            }

            cards_pyme = [
                {"label": "Ingresos Totales", "value": total_ventas},
                {"label": "Pedidos", "value": total_pedidos},
                {"label": "Clientes Únicos", "value": clientes_unicos},
                {"label": "Tasa de Conversión", "value": tasa_conversion},
            ]

            stats_pyme = {
                "kpis": metricas.get_kpis(),
                "ventas_over_time": metricas.get_sales_over_time(),
                "top_productos": metricas.get_top_products(),
                "ventas_por_region": metricas.get_sales_by_region(),
            }

        payload["stats"] = stats_pyme
        payload["summary"] = resumen_pyme
        payload["cards"] = cards_pyme
        payload["filters"] = {}

    return jsonify(payload)


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
        categoria=categorias or None,
        distrito=distrito,
        estado=estado_param,
        satisfactorio=args.get("satisfactorio", type=lambda v: str(v).lower() == "true"),
    )

    if not puntos:
        puntos = _demo_heatmap(args.get("tipo_ticket", "municipio"))

    payload: dict[str, object] = {"heatmap": puntos}

    _augment_heatmap_payload(payload)

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
    return "", 204


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
    categoria = categoria_values or None

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

    heatmap = puntos or _demo_heatmap(tipo)

    respuesta: dict[str, object] = {"heatmap": heatmap}

    _augment_heatmap_payload(respuesta)

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
