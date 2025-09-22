"""Utilities to build rich analytics for municipal tickets and suggestions."""

from __future__ import annotations

from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Iterable, Sequence

from sqlalchemy import Float, and_, case, cast, func, or_

from models import (
    MunicipioTicket,
    SugerenciaCiudadano,
    TicketComentario,
    TicketSatisfaccion,
    db,
)
from utils.time_utils import get_local_now


# Estados que consideramos como tickets cerrados/resueltos para las métricas.
_CLOSED_STATES: set[str] = {"cerrado", "resuelto", "completado"}


@dataclass(frozen=True)
class _TimeSeriesPoint:
    label: str
    total: int
    abiertos: int
    cerrados: int


def _current_dialect_name(default: str = "sqlite") -> str:
    """Return the lower-cased SQL dialect name for the active session."""

    bind = getattr(db.session, "bind", None)
    if bind is not None:
        name = getattr(getattr(bind, "dialect", None), "name", None)
        if name:
            return name.lower()

    with suppress(Exception):
        bind = db.session.get_bind()
        name = getattr(getattr(bind, "dialect", None), "name", None)
        if name:
            return name.lower()

    with suppress(Exception):
        engine = db.engine
        name = getattr(getattr(engine, "dialect", None), "name", None)
        if name:
            return name.lower()

    with suppress(Exception):
        engine = db.get_engine()
        name = getattr(getattr(engine, "dialect", None), "name", None)
        if name:
            return name.lower()

    return default.lower()


def _uses_sqlite() -> bool:
    """Return True when the active SQL dialect is SQLite."""

    dialect = _current_dialect_name()
    return dialect.split("+", 1)[0] == "sqlite"


def _epoch_seconds(column):
    """Return an expression that converts a timestamp to epoch seconds."""

    if _uses_sqlite():
        return func.strftime("%s", column)

    return func.extract("epoch", column)


def _build_state_map(rows: Iterable[tuple[str | None, int]]) -> dict[str, int]:
    """Create a normalized mapping of estado => cantidad."""

    normalized: dict[str, int] = defaultdict(int)
    for estado, cantidad in rows:
        key = (estado or "desconocido").strip().lower()
        normalized[key] += int(cantidad or 0)
    return dict(normalized)


def _series_to_dict(points: Sequence[_TimeSeriesPoint]) -> list[dict]:
    return [
        {
            "label": point.label,
            "total": point.total,
            "abiertos": point.abiertos,
            "cerrados": point.cerrados,
        }
        for point in points
    ]


def _summarize_hours(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "promedio_horas": 0.0,
            "mediana_horas": 0.0,
            "minimo_horas": 0.0,
            "maximo_horas": 0.0,
            "porcentaje_24h": 0.0,
        }

    promedio = mean(values)
    mediana = median(values)
    minimo = min(values)
    maximo = max(values)
    dentro_24 = sum(1 for value in values if value <= 24)
    porcentaje_24 = round(dentro_24 * 100 / len(values), 2)

    return {
        "promedio_horas": round(promedio, 2),
        "mediana_horas": round(mediana, 2),
        "minimo_horas": round(minimo, 2),
        "maximo_horas": round(maximo, 2),
        "porcentaje_24h": porcentaje_24,
    }


def _compute_time_to_first_response(municipio_id: int) -> list[float]:
    """Return the response times in hours for tickets con primera respuesta."""

    first_admin_comment = (
        db.session.query(
            TicketComentario.municipio_ticket_id.label("ticket_id"),
            func.min(TicketComentario.fecha).label("fecha_respuesta"),
        )
        .filter(
            TicketComentario.municipio_ticket_id.isnot(None),
            TicketComentario.es_admin.is_(True),
        )
        .group_by(TicketComentario.municipio_ticket_id)
        .subquery()
    )

    response_seconds_expr = (
        cast(_epoch_seconds(first_admin_comment.c.fecha_respuesta), Float)
        - cast(_epoch_seconds(MunicipioTicket.fecha), Float)
    )

    rows = (
        db.session.query(response_seconds_expr.label("segundos"))
        .join(first_admin_comment, first_admin_comment.c.ticket_id == MunicipioTicket.id)
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .all()
    )

    horas = [max(0.0, float(row.segundos) / 3600.0) for row in rows if row.segundos is not None]
    return horas


def _compute_time_to_close(municipio_id: int) -> list[float]:
    """Return closure times in hours for closed tickets."""

    cierre_segundos_expr = (
        cast(_epoch_seconds(MunicipioTicket.ultima_actividad), Float)
        - cast(_epoch_seconds(MunicipioTicket.fecha), Float)
    )

    rows = (
        db.session.query(cierre_segundos_expr.label("segundos"))
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.estado.in_(_CLOSED_STATES),
            MunicipioTicket.ultima_actividad.isnot(None),
        )
        .all()
    )

    horas = [max(0.0, float(row.segundos) / 3600.0) for row in rows if row.segundos is not None]
    return horas


def _build_month_expression(column=MunicipioTicket.fecha):
    if _uses_sqlite():
        return func.strftime("%Y-%m", column)
    return func.to_char(column, "YYYY-MM")


def _build_day_expression(column=MunicipioTicket.fecha):
    if _uses_sqlite():
        return func.strftime("%Y-%m-%d", column)
    return func.to_char(column, "YYYY-MM-DD")


def _normalize_label(value: str | None, fallback: str) -> str:
    normalized = (value or "").strip()
    return normalized if normalized else fallback


def build_stats_for_municipio(
    municipio_id: int | None,
    *,
    now: datetime | None = None,
) -> dict:
    """Return detailed analytics for tickets and citizen suggestions."""

    if not municipio_id:
        return {
            "resumen": {
                "total": 0,
                "abiertos": 0,
                "cerrados": 0,
                "sin_resolver": 0,
                "nuevos": 0,
                "en_proceso": 0,
                "en_vivo": 0,
                "esperando": 0,
                "resueltos": 0,
                "expirados": 0,
                "promedio_satisfaccion": 0.0,
                "respuestas_satisfaccion": 0,
                "tasa_resolucion": 0.0,
                "sla_24h": 0.0,
            },
            "estados": [],
            "por_categoria": [],
            "por_distrito": [],
            "por_canal": [],
            "tendencia_mensual": [],
            "tendencia_semanal": [],
            "tiempos_respuesta": _summarize_hours([]),
            "tiempos_cierre": _summarize_hours([]),
            "backlog": {"menos_72h": 0, "entre_3_y_7_dias": 0, "mas_7_dias": 0},
            "geolocalizacion": {"con_coordenadas": 0, "por_categoria": []},
            "satisfaccion": {
                "promedio": 0.0,
                "respuestas": 0,
                "distribucion": [],
            },
            "sugerencias": {
                "total": 0,
                "por_estado": [],
                "por_categoria": [],
                "tendencia_mensual": [],
            },
        }

    ahora = now or get_local_now()

    # --- Tickets por estado -------------------------------------------------
    state_rows = (
        db.session.query(
            MunicipioTicket.estado,
            func.count(MunicipioTicket.id),
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .group_by(MunicipioTicket.estado)
        .all()
    )
    state_map = _build_state_map(state_rows)

    total_tickets = sum(state_map.values())
    cerrados = sum(count for estado, count in state_map.items() if estado in _CLOSED_STATES)
    abiertos = sum(count for estado, count in state_map.items() if estado not in _CLOSED_STATES)

    resumen_base = {
        "total": total_tickets,
        "abiertos": abiertos,
        "cerrados": cerrados,
        "sin_resolver": abiertos,
        "nuevos": state_map.get("nuevo", 0),
        "en_proceso": state_map.get("en_proceso", 0),
        "en_vivo": state_map.get("en_vivo", 0),
        "esperando": state_map.get("esperando_agente_en_vivo", 0),
        "resueltos": cerrados,
    }

    expirados = (
        db.session.query(func.count(MunicipioTicket.id))
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            or_(
                MunicipioTicket.estado.is_(None),
                ~MunicipioTicket.estado.in_(list(_CLOSED_STATES)),
            ),
            MunicipioTicket.fecha < (ahora - timedelta(days=30)),
        )
        .scalar()
        or 0
    )
    resumen_base["expirados"] = expirados

    # --- Estadísticas por categoría ----------------------------------------
    categoria_rows = (
        db.session.query(
            MunicipioTicket.categoria,
            func.count(MunicipioTicket.id).label("total"),
            func.sum(
                case((MunicipioTicket.estado.in_(list(_CLOSED_STATES)), 1), else_=0)
            ).label("cerrados"),
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .group_by(MunicipioTicket.categoria)
        .all()
    )

    categoria_satisfaccion_rows = (
        db.session.query(
            MunicipioTicket.categoria,
            func.avg(TicketSatisfaccion.puntuacion).label("promedio"),
            func.count(TicketSatisfaccion.id).label("respuestas"),
        )
        .join(
            TicketSatisfaccion,
            and_(
                TicketSatisfaccion.ticket_id == MunicipioTicket.id,
                TicketSatisfaccion.tipo == "municipio",
            ),
            isouter=True,
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .group_by(MunicipioTicket.categoria)
        .all()
    )

    satisfaccion_por_categoria = {
        _normalize_label(row.categoria, "Sin categoría"): {
            "promedio": round(float(row.promedio), 2) if row.promedio is not None else 0.0,
            "respuestas": int(row.respuestas or 0),
        }
        for row in categoria_satisfaccion_rows
    }

    por_categoria = []
    for row in categoria_rows:
        categoria = _normalize_label(row.categoria, "Sin categoría")
        total = int(row.total or 0)
        cerrados_categoria = int(row.cerrados or 0)
        por_categoria.append(
            {
                "categoria": categoria,
                "total": total,
                "abiertos": max(total - cerrados_categoria, 0),
                "cerrados": cerrados_categoria,
                "satisfaccion": satisfaccion_por_categoria.get(
                    categoria,
                    {"promedio": 0.0, "respuestas": 0},
                ),
            }
        )

    # --- Distritos ---------------------------------------------------------
    distrito_rows = (
        db.session.query(
            MunicipioTicket.distrito,
            func.count(MunicipioTicket.id).label("total"),
            func.sum(
                case((MunicipioTicket.estado.in_(list(_CLOSED_STATES)), 1), else_=0)
            ).label("cerrados"),
        )
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.distrito.isnot(None),
        )
        .group_by(MunicipioTicket.distrito)
        .all()
    )

    por_distrito = [
        {
            "distrito": _normalize_label(row.distrito, "Sin distrito"),
            "total": int(row.total or 0),
            "abiertos": max(int(row.total or 0) - int(row.cerrados or 0), 0),
            "cerrados": int(row.cerrados or 0),
        }
        for row in distrito_rows
    ]

    # --- Canal de ingreso --------------------------------------------------
    canal_rows = (
        db.session.query(
            MunicipioTicket.canal_ingreso,
            func.count(MunicipioTicket.id),
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .group_by(MunicipioTicket.canal_ingreso)
        .all()
    )
    por_canal = [
        {
            "canal": _normalize_label(row.canal_ingreso, "Sin especificar"),
            "total": int(row[1] or 0),
        }
        for row in canal_rows
    ]

    # --- Tendencias temporales --------------------------------------------
    month_col = _build_month_expression(MunicipioTicket.fecha).label("mes")
    month_rows = (
        db.session.query(
            month_col,
            func.count(MunicipioTicket.id).label("total"),
            func.sum(
                case((MunicipioTicket.estado.in_(list(_CLOSED_STATES)), 1), else_=0)
            ).label("cerrados"),
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .group_by(month_col)
        .order_by(month_col)
        .all()
    )

    tendencia_mensual = _series_to_dict(
        [
            _TimeSeriesPoint(
                label=row.mes,
                total=int(row.total or 0),
                cerrados=int(row.cerrados or 0),
                abiertos=max(int(row.total or 0) - int(row.cerrados or 0), 0),
            )
            for row in month_rows
        ]
    )

    day_col = _build_day_expression(MunicipioTicket.fecha).label("dia")
    semana_inicio = ahora - timedelta(days=6)
    week_rows = (
        db.session.query(
            day_col,
            func.count(MunicipioTicket.id).label("total"),
            func.sum(
                case((MunicipioTicket.estado.in_(list(_CLOSED_STATES)), 1), else_=0)
            ).label("cerrados"),
        )
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.fecha >= semana_inicio,
        )
        .group_by(day_col)
        .order_by(day_col)
        .all()
    )

    tendencia_semanal = _series_to_dict(
        [
            _TimeSeriesPoint(
                label=row.dia,
                total=int(row.total or 0),
                cerrados=int(row.cerrados or 0),
                abiertos=max(int(row.total or 0) - int(row.cerrados or 0), 0),
            )
            for row in week_rows
        ]
    )

    # --- Tiempos de respuesta y cierre ------------------------------------
    tiempos_respuesta_horas = _compute_time_to_first_response(municipio_id)
    tiempos_cierre_horas = _compute_time_to_close(municipio_id)

    tiempos_respuesta = _summarize_hours(tiempos_respuesta_horas)
    tiempos_cierre = _summarize_hours(tiempos_cierre_horas)

    # SLA basado en respuestas dentro de las primeras 24 horas.
    resumen_base["sla_24h"] = tiempos_respuesta["porcentaje_24h"]

    # --- Backlog -----------------------------------------------------------
    menos_72 = (
        db.session.query(func.count(MunicipioTicket.id))
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            or_(
                MunicipioTicket.estado.is_(None),
                ~MunicipioTicket.estado.in_(list(_CLOSED_STATES)),
            ),
            MunicipioTicket.fecha >= (ahora - timedelta(hours=72)),
        )
        .scalar()
        or 0
    )
    entre_3_y_7 = (
        db.session.query(func.count(MunicipioTicket.id))
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            or_(
                MunicipioTicket.estado.is_(None),
                ~MunicipioTicket.estado.in_(list(_CLOSED_STATES)),
            ),
            MunicipioTicket.fecha < (ahora - timedelta(hours=72)),
            MunicipioTicket.fecha >= (ahora - timedelta(days=7)),
        )
        .scalar()
        or 0
    )
    mas_7 = (
        db.session.query(func.count(MunicipioTicket.id))
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            or_(
                MunicipioTicket.estado.is_(None),
                ~MunicipioTicket.estado.in_(list(_CLOSED_STATES)),
            ),
            MunicipioTicket.fecha < (ahora - timedelta(days=7)),
        )
        .scalar()
        or 0
    )

    backlog = {
        "menos_72h": int(menos_72),
        "entre_3_y_7_dias": int(entre_3_y_7),
        "mas_7_dias": int(mas_7),
    }

    # --- Geolocalización ---------------------------------------------------
    geolocalizados_total = (
        db.session.query(func.count(MunicipioTicket.id))
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.latitud.isnot(None),
            MunicipioTicket.longitud.isnot(None),
        )
        .scalar()
        or 0
    )

    geolocalizados_por_categoria_rows = (
        db.session.query(
            MunicipioTicket.categoria,
            func.count(MunicipioTicket.id),
        )
        .filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.latitud.isnot(None),
            MunicipioTicket.longitud.isnot(None),
        )
        .group_by(MunicipioTicket.categoria)
        .all()
    )

    geolocalizados_por_categoria = [
        {
            "categoria": _normalize_label(row.categoria, "Sin categoría"),
            "total": int(row[1] or 0),
        }
        for row in geolocalizados_por_categoria_rows
    ]

    geolocalizacion = {
        "con_coordenadas": int(geolocalizados_total),
        "por_categoria": geolocalizados_por_categoria,
    }

    # --- Satisfacción global ----------------------------------------------
    satisfaccion_global = (
        db.session.query(
            func.avg(TicketSatisfaccion.puntuacion),
            func.count(TicketSatisfaccion.id),
        )
        .join(
            MunicipioTicket,
            and_(
                TicketSatisfaccion.ticket_id == MunicipioTicket.id,
                TicketSatisfaccion.tipo == "municipio",
            ),
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .first()
    )

    promedio_satisfaccion = 0.0
    respuestas_satisfaccion = 0
    if satisfaccion_global:
        promedio_satisfaccion = (
            round(float(satisfaccion_global[0]), 2) if satisfaccion_global[0] is not None else 0.0
        )
        respuestas_satisfaccion = int(satisfaccion_global[1] or 0)

    resumen_base["promedio_satisfaccion"] = promedio_satisfaccion
    resumen_base["respuestas_satisfaccion"] = respuestas_satisfaccion
    resumen_base["tasa_resolucion"] = round(
        (cerrados / total_tickets * 100) if total_tickets else 0.0,
        2,
    )

    # --- Distribución de satisfacción -------------------------------------
    satisfaccion_distribucion_rows = (
        db.session.query(
            TicketSatisfaccion.puntuacion,
            func.count(TicketSatisfaccion.id),
        )
        .join(
            MunicipioTicket,
            and_(
                TicketSatisfaccion.ticket_id == MunicipioTicket.id,
                TicketSatisfaccion.tipo == "municipio",
            ),
        )
        .filter(MunicipioTicket.municipio_id == municipio_id)
        .group_by(TicketSatisfaccion.puntuacion)
        .order_by(TicketSatisfaccion.puntuacion)
        .all()
    )

    satisfaccion_distribucion = [
        {"puntuacion": int(row[0]), "total": int(row[1] or 0)}
        for row in satisfaccion_distribucion_rows
    ]

    # --- Sugerencias -------------------------------------------------------
    sugerencia_rows = (
        db.session.query(
            func.count(SugerenciaCiudadano.id),
        )
        .filter(SugerenciaCiudadano.municipio_id == municipio_id)
        .first()
    )
    sugerencias_total = int((sugerencia_rows or (0,))[0] or 0)

    sugerencias_por_estado_rows = (
        db.session.query(
            SugerenciaCiudadano.estado,
            func.count(SugerenciaCiudadano.id),
        )
        .filter(SugerenciaCiudadano.municipio_id == municipio_id)
        .group_by(SugerenciaCiudadano.estado)
        .all()
    )
    sugerencias_por_estado = [
        {
            "estado": _normalize_label(row.estado, "Sin estado"),
            "total": int(row[1] or 0),
        }
        for row in sugerencias_por_estado_rows
    ]

    sugerencias_por_categoria_rows = (
        db.session.query(
            SugerenciaCiudadano.categoria,
            func.count(SugerenciaCiudadano.id),
        )
        .filter(SugerenciaCiudadano.municipio_id == municipio_id)
        .group_by(SugerenciaCiudadano.categoria)
        .all()
    )
    sugerencias_por_categoria = [
        {
            "categoria": _normalize_label(row.categoria, "Sin categoría"),
            "total": int(row[1] or 0),
        }
        for row in sugerencias_por_categoria_rows
    ]

    sugerencia_month_col = _build_month_expression(SugerenciaCiudadano.fecha).label("mes")
    sugerencias_mensual_rows = (
        db.session.query(
            sugerencia_month_col,
            func.count(SugerenciaCiudadano.id),
        )
        .filter(SugerenciaCiudadano.municipio_id == municipio_id)
        .group_by(sugerencia_month_col)
        .order_by(sugerencia_month_col)
        .all()
    )
    sugerencias_tendencia = [
        {"label": row.mes, "total": int(row[1] or 0)} for row in sugerencias_mensual_rows
    ]

    # --- Estados como porcentaje ------------------------------------------
    estados_detalle = []
    for estado, cantidad in state_map.items():
        porcentaje = round((cantidad / total_tickets * 100), 2) if total_tickets else 0.0
        estados_detalle.append(
            {
                "estado": estado,
                "total": cantidad,
                "porcentaje": porcentaje,
            }
        )

    datos = {
        "resumen": resumen_base,
        "estados": estados_detalle,
        "por_categoria": por_categoria,
        "por_distrito": por_distrito,
        "por_canal": por_canal,
        "tendencia_mensual": tendencia_mensual,
        "tendencia_semanal": tendencia_semanal,
        "tiempos_respuesta": tiempos_respuesta,
        "tiempos_cierre": tiempos_cierre,
        "backlog": backlog,
        "geolocalizacion": geolocalizacion,
        "satisfaccion": {
            "promedio": promedio_satisfaccion,
            "respuestas": respuestas_satisfaccion,
            "distribucion": satisfaccion_distribucion,
        },
        "sugerencias": {
            "total": sugerencias_total,
            "por_estado": sugerencias_por_estado,
            "por_categoria": sugerencias_por_categoria,
            "tendencia_mensual": sugerencias_tendencia,
        },
    }

    return datos

