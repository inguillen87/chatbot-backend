"""Analytics helpers for surveys."""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import joinedload

from database import db
from models import EncEncuesta, EncRespuesta, EncPregunta, EncRespuestaDetalle
from services.encuestas_service import (
    EncuestaError,
    get_encuesta,
    get_public_encuesta,
    _parse_datetime,
    _resolve_geo_metadata_for_tenant,
)
from utils.heatmap import (
    build_feature_collection,
    compute_heatmap_cell_id,
    compute_heatmap_centroid,
    enrich_heatmap_cells,
    enrich_heatmap_points,
)
from utils.map_config import get_map_config


_SINGLE_CHOICE_TYPES = {
    "opcion_unica",
    "single_choice",
    "single-choice",
    "singlechoice",
    "single",
    "radio",
}

_MULTIPLE_CHOICE_TYPES = {
    "opcion_multiple",
    "multiple_choice",
    "multiple-choice",
    "multiple",
    "checkbox",
    "check",
    "multi_select",
    "multi-select",
    "multiselect",
}

_TEXT_TYPES = {
    "abierta",
    "text",
    "texto",
    "open_text",
    "open-text",
    "open",
}


def _normalize_question_type(raw: Optional[str]) -> str:
    if raw is None:
        return "single_choice"

    text = str(raw).strip().lower()
    if not text:
        return "single_choice"
    if text in _SINGLE_CHOICE_TYPES:
        return "single_choice"
    if text in _MULTIPLE_CHOICE_TYPES:
        return "multiple_choice"
    if text in _TEXT_TYPES:
        return "text"
    return text


def _apply_filters(query, filtros: Optional[Dict[str, Any]]):
    if not filtros:
        return query
    if filtros.get("desde"):
        desde = _parse_datetime(filtros["desde"])
        if desde:
            query = query.filter(EncRespuesta.submitted_at >= desde)
    if filtros.get("hasta"):
        hasta = _parse_datetime(filtros["hasta"])
        if hasta:
            query = query.filter(EncRespuesta.submitted_at <= hasta)
    if filtros.get("canal"):
        query = query.filter(EncRespuesta.canal == filtros["canal"])
    if filtros.get("utm_source"):
        query = query.filter(EncRespuesta.utm_source == filtros["utm_source"])
    if filtros.get("utm_campaign"):
        query = query.filter(EncRespuesta.utm_campaign == filtros["utm_campaign"])

    def _apply_text_filter(column, key: str):
        values = filtros.get(key)
        if not values:
            return
        if isinstance(values, str):
            query_local = query.filter(column == values)
        else:
            query_local = query.filter(column.in_(list(values)))
        return query_local

    for column, key in (
        (EncRespuesta.genero, "genero"),
        (EncRespuesta.rango_etario, "rango_etario"),
        (EncRespuesta.barrio, "barrio"),
        (EncRespuesta.ciudad, "ciudad"),
        (EncRespuesta.provincia, "provincia"),
        (EncRespuesta.pais, "pais"),
    ):
        filtered = _apply_text_filter(column, key)
        if filtered is not None:
            query = filtered
    return query


def _collect_respuestas(encuesta: EncEncuesta, filtros: Optional[Dict[str, Any]]):
    query = EncRespuesta.query.options(joinedload(EncRespuesta.detalles)).filter_by(encuesta_id=encuesta.id)
    query = _apply_filters(query, filtros)
    return query.order_by(EncRespuesta.submitted_at.asc()).all()


def _top_counter(counter: Counter, limit: int = 10) -> List[Dict[str, Any]]:
    return [
        {"label": label, "value": count}
        for label, count in counter.most_common(limit)
    ]


def _counter_to_list(counter: Counter) -> List[Dict[str, Any]]:
    """Return a stable list representation for chart-friendly payloads."""

    # ``Counter`` preserves insertion order starting from Python 3.7, but we
    # still sort descending to match the behaviour of ``most_common`` which the
    # frontend was already using for other widgets.
    return [
        {"label": label, "value": counter[label]}
        for label in sorted(counter.keys(), key=lambda key: counter[key], reverse=True)
    ]


DEFAULT_HEATMAP_RESOLUTION = 8


def _build_synthetic_heatmap_points(
    encuesta: EncEncuesta,
    respuestas: Sequence[EncRespuesta],
) -> List[Dict[str, Any]]:
    """Build fallback heatmap points when responses lack explicit coordinates."""

    tenant_geo = _resolve_geo_metadata_for_tenant(encuesta.tenant_id)
    center = (tenant_geo or {}).get("center") if isinstance(tenant_geo, dict) else None
    if not center or len(center) < 2:
        center = [-58.3816, -34.6037]

    base_lng = float(center[0])
    base_lat = float(center[1])
    synthetic_points: List[Dict[str, Any]] = []

    for idx, respuesta in enumerate(respuestas):
        if respuesta.lat is not None and respuesta.lng is not None:
            continue

        # deterministic spread around tenant center so repeated requests are stable
        lat = base_lat + (((idx % 7) - 3) * 0.0025)
        lng = base_lng + (((idx % 11) - 5) * 0.0025)
        submitted_at = respuesta.submitted_at
        synthetic_points.append(
            {
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "w": 0.5,
                "barrio": respuesta.barrio,
                "ciudad": respuesta.ciudad,
                "provincia": respuesta.provincia,
                "pais": respuesta.pais,
                "canal": respuesta.canal,
                "synthetic": True,
                "submitted_at": submitted_at.isoformat() if submitted_at else None,
            }
        )

    return synthetic_points


def _aggregate_heatmap_cells(
    respuestas: Sequence[EncRespuesta],
    *,
    resolution: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    points: List[Dict[str, Any]] = []
    cells: Dict[str, Dict[str, Any]] = {}
    effective_resolution = resolution or DEFAULT_HEATMAP_RESOLUTION

    for respuesta in respuestas:
        if respuesta.lat is None or respuesta.lng is None:
            continue

        lat = float(respuesta.lat)
        lng = float(respuesta.lng)
        submitted_at = respuesta.submitted_at
        points.append(
            {
                "lat": lat,
                "lng": lng,
                "w": 1.0,
                "barrio": respuesta.barrio,
                "ciudad": respuesta.ciudad,
                "provincia": respuesta.provincia,
                "pais": respuesta.pais,
                "canal": respuesta.canal,
                "submitted_at": submitted_at.isoformat() if submitted_at else None,
            }
        )

        cell_id = compute_heatmap_cell_id(lat, lng, effective_resolution)
        cell = cells.setdefault(
            cell_id,
            {
                "count": 0,
                "lat_sum": 0.0,
                "lng_sum": 0.0,
                "barrios": defaultdict(int),
                "canales": defaultdict(int),
            },
        )
        cell["count"] += 1
        cell["lat_sum"] += lat
        cell["lng_sum"] += lng
        if respuesta.barrio:
            cell["barrios"][respuesta.barrio] += 1
        if respuesta.canal:
            cell["canales"][respuesta.canal] += 1

    cells_payload: List[Dict[str, Any]] = []
    for cell_id, data in cells.items():
        centroid_lat, centroid_lng = compute_heatmap_centroid(
            cell_id,
            lat_sum=data["lat_sum"],
            lng_sum=data["lng_sum"],
            count=data["count"],
        )
        cells_payload.append(
            {
                "cell_id": cell_id,
                "count": data["count"],
                "centroid_lat": round(centroid_lat, 6) if centroid_lat is not None else None,
                "centroid_lon": round(centroid_lng, 6) if centroid_lng is not None else None,
                "barrios": dict(
                    sorted(data["barrios"].items(), key=lambda item: item[1], reverse=True)
                ),
                "canales": dict(
                    sorted(data["canales"].items(), key=lambda item: item[1], reverse=True)
                ),
            }
        )

    cells_payload.sort(key=lambda cell: cell["count"], reverse=True)
    enrich_heatmap_points(
        points,
        property_keys=("barrio", "ciudad", "provincia", "pais", "canal"),
    )
    enrich_heatmap_cells(
        cells_payload,
        property_keys=("barrios", "canales"),
    )
    return points, cells_payload


def _build_map_filter(points: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Return filter metadata for the heatmap payload.

    The modern admin dashboard can expose dynamic filters for map layers and it
    expects the backend to provide the available values with usage counts so it
    can render the controls without extra round-trips.  We derive the
    statistics from the already-normalised points list to avoid additional
    database queries.
    """

    filter_counters: Dict[str, Counter] = {
        "canal": Counter(),
        "barrio": Counter(),
        "ciudad": Counter(),
        "provincia": Counter(),
        "pais": Counter(),
    }

    for point in points:
        if not isinstance(point, Mapping):  # type: ignore[arg-type]
            continue
        for key, counter in filter_counters.items():
            raw_value = point.get(key)
            if raw_value is None:
                continue
            values: Iterable[Any]
            if isinstance(raw_value, (list, tuple, set)):
                values = raw_value
            else:
                values = (raw_value,)
            for value in values:
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    counter[text] += 1

    options: Dict[str, List[Dict[str, Any]]] = {}
    for key, counter in filter_counters.items():
        if not counter:
            continue
        options[key] = [
            {"label": label, "value": label, "count": count}
            for label, count in counter.most_common()
        ]

    keys = sorted(options.keys())
    return {
        "available": bool(options),
        "keys": keys,
        "options": options,
    }


def _build_heatmap_metadata(
    encuesta: EncEncuesta,
    points: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    tenant_geo = _resolve_geo_metadata_for_tenant(encuesta.tenant_id)
    bounds = None
    if points:
        min_lat = min(point["lat"] for point in points)
        max_lat = max(point["lat"] for point in points)
        min_lng = min(point["lng"] for point in points)
        max_lng = max(point["lng"] for point in points)
        bounds = [min_lng, min_lat, max_lng, max_lat]

    metadata = {
        "encuesta_id": encuesta.id,
        "total_points": len(points),
        "tenant_id": encuesta.tenant_id,
        "bounds": bounds,
        "tenant_bounds": tenant_geo.get("bounds") if tenant_geo else None,
        "tenant_center": tenant_geo.get("center") if tenant_geo else None,
    }
    return metadata


def get_summary(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    total = len(respuestas)

    opciones_por_pregunta = defaultdict(Counter)
    textos_abiertos: Dict[int, List[str]] = defaultdict(list)
    canales = Counter()
    utm = Counter()
    participantes_unicos: set[str] = set()
    generos = Counter()
    rangos_etarios = Counter()
    barrios = Counter()
    ciudades = Counter()
    provincias = Counter()
    paises = Counter()
    edades: List[int] = []

    preguntas_obligatorias = {
        pregunta.id
        for pregunta in encuesta.preguntas
        if getattr(pregunta, "obligatoria", False)
    }
    respuestas_completas = 0

    for respuesta in respuestas:
        canales[respuesta.canal or "sin_canal"] += 1
        utm_key = f"{respuesta.utm_source or 'n/a'}|{respuesta.utm_campaign or 'n/a'}"
        utm[utm_key] += 1

        fingerprint = (
            respuesta.huella_unica
            or (respuesta.user_id and f"user:{respuesta.user_id}")
            or (respuesta.dni and f"dni:{respuesta.dni.strip()}")
            or (respuesta.phone and f"phone:{respuesta.phone.strip()}")
            or (respuesta.ip and f"ip:{respuesta.ip}")
        )
        participantes_unicos.add(str(fingerprint or f"anon:{respuesta.id}"))

        if respuesta.genero:
            generos[respuesta.genero] += 1
        if respuesta.rango_etario:
            rangos_etarios[respuesta.rango_etario] += 1
        if respuesta.barrio:
            barrios[respuesta.barrio] += 1
        if respuesta.ciudad:
            ciudades[respuesta.ciudad] += 1
        if respuesta.provincia:
            provincias[respuesta.provincia] += 1
        if respuesta.pais:
            paises[respuesta.pais] += 1
        if isinstance(respuesta.edad, int):
            edades.append(respuesta.edad)

        detalles_por_pregunta = defaultdict(list)
        for detalle in respuesta.detalles:
            if detalle.opcion_id:
                opciones_por_pregunta[detalle.pregunta_id][detalle.opcion_id] += 1
            detalles_por_pregunta[detalle.pregunta_id].append(detalle)
            if detalle.texto_libre:
                textos_abiertos[detalle.pregunta_id].append(detalle.texto_libre)

        if preguntas_obligatorias:
            if all(detalles_por_pregunta.get(pid) for pid in preguntas_obligatorias):
                respuestas_completas += 1
        else:
            respuestas_completas += 1

    preguntas_summary = []
    for pregunta in encuesta.preguntas:
        normalized_tipo = _normalize_question_type(pregunta.tipo)
        pregunta_data = {
            "pregunta_id": pregunta.id,
            "texto": pregunta.texto,
            "tipo": normalized_tipo,
            "tipo_interno": pregunta.tipo,
            "total_respuestas": total,
        }
        if normalized_tipo in {"single_choice", "multiple_choice"}:
            opciones = []
            for opcion in pregunta.opciones:
                conteo = opciones_por_pregunta[pregunta.id][opcion.id]
                porcentaje = (conteo / total * 100) if total else 0
                opciones.append(
                    {
                        "opcion_id": opcion.id,
                        "texto": opcion.texto,
                        "conteo": conteo,
                        "value": conteo,
                        "porcentaje": round(porcentaje, 2),
                    }
                )
            pregunta_data["opciones"] = opciones
            pregunta_data["series"] = [
                {"label": opcion["texto"], "value": opcion["conteo"]}
                for opcion in opciones
            ]
        elif normalized_tipo == "text":
            muestras = textos_abiertos.get(pregunta.id, [])[:20]
            pregunta_data["muestras_texto"] = muestras
            # Frontend widgets expect ``opciones`` to exist so they can iterate
            # without special casing preguntas de texto libre.
            pregunta_data["opciones"] = []
            pregunta_data["series"] = []
        else:
            # Preserve backwards compatibility for unexpected question types by
            # exposing aggregated option counts when available.
            opciones = []
            for opcion in pregunta.opciones:
                conteo = opciones_por_pregunta[pregunta.id][opcion.id]
                porcentaje = (conteo / total * 100) if total else 0
                opciones.append(
                    {
                        "opcion_id": opcion.id,
                        "texto": opcion.texto,
                        "conteo": conteo,
                        "value": conteo,
                        "porcentaje": round(porcentaje, 2),
                    }
                )
            pregunta_data["opciones"] = opciones
            pregunta_data["series"] = [
                {"label": opcion["texto"], "value": opcion["conteo"]}
                for opcion in opciones
            ]
        preguntas_summary.append(pregunta_data)

    canales_list = [
        {
            "canal": canal,
            "label": canal,
            "conteo": count,
            "value": count,
        }
        for canal, count in sorted(canales.items(), key=lambda item: item[1], reverse=True)
    ]
    canales_map = {canal: count for canal, count in canales.items()}
    utm_data = []
    for key, count in utm.items():
        source, campaign = key.split("|", 1)
        utm_data.append({"utm_source": source, "utm_campaign": campaign, "conteo": count})

    tasa_completitud = (respuestas_completas / total * 100) if total else 0.0

    edades_ordenadas = sorted(edades)
    edad_promedio = round(mean(edades_ordenadas), 2) if edades_ordenadas else None
    edad_mediana = median(edades_ordenadas) if edades_ordenadas else None

    def _percentile(values: List[int], pct: float) -> Optional[float]:
        if not values:
            return None
        if len(values) == 1:
            return float(values[0])
        index = (len(values) - 1) * pct / 100.0
        lower = int(index)
        upper = min(lower + 1, len(values) - 1)
        fraction = index - lower
        return round(values[lower] + (values[upper] - values[lower]) * fraction, 2)

    edad_p90 = _percentile(edades_ordenadas, 90.0)

    territorio_breakdown = {
        "barrios": _top_counter(barrios),
        "ciudades": _top_counter(ciudades),
        "provincias": _top_counter(provincias),
        "paises": _top_counter(paises),
    }

    territorio_sections = [
        {
            "key": key,
            "label": key.capitalize(),
            "series": values,
        }
        for key, values in territorio_breakdown.items()
    ]

    demografia = {
        # ``genero`` and ``rango_etario`` now expose array payloads to align
        # with the modern admin dashboard, while the ``*_map`` aliases keep the
        # dictionary structure for legacy consumers and regression tests.
        "genero": _counter_to_list(generos),
        "genero_series": _counter_to_list(generos),
        "genero_map": dict(generos),
        "rango_etario": _counter_to_list(rangos_etarios),
        "rango_etario_series": _counter_to_list(rangos_etarios),
        "rango_etario_map": dict(rangos_etarios),
        "edad": {
            "promedio": edad_promedio,
            "mediana": edad_mediana,
            "p90": edad_p90,
            "muestra": len(edades_ordenadas),
        },
        # ``territorio`` now follows the array-first contract expected by the
        # modern admin dashboard (each entry already exposes ``series`` so the
        # frontend can map safely), while ``territorio_map`` keeps backwards
        # compatibility for legacy consumers and regression tests.
        "territorio": territorio_sections,
        "territorio_map": territorio_breakdown,
    }

    return {
        "encuesta_id": encuesta.id,
        "total_respuestas": total,
        "participantes_unicos": len(participantes_unicos),
        "respuestas_completas": respuestas_completas,
        "respuestas_incompletas": max(total - respuestas_completas, 0),
        "tasa_completitud": round(tasa_completitud, 2),
        "preguntas": preguntas_summary,
        "canales": canales_list,
        "canales_map": canales_map,
        "utm": utm_data,
        "demografia": demografia,
    }


def get_timeseries(encuesta_id: int, granularity: str = "day", filtros: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)

    buckets = Counter()
    for respuesta in respuestas:
        dt = respuesta.submitted_at or datetime.now(timezone.utc)
        dt = dt.astimezone(timezone.utc)
        if granularity == "hour":
            bucket = dt.replace(minute=0, second=0, microsecond=0)
        else:
            bucket = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets[bucket] += 1

    series = [
        {"fecha": bucket.isoformat(), "total": buckets[bucket]} for bucket in sorted(buckets.keys())
    ]
    return series




def get_forecast(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    window_minutes: int = 10,
    horizon_minutes: int = 60,
) -> Dict[str, Any]:
    """Build a lightweight short-term projection from minute-level activity."""

    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    now = datetime.now(timezone.utc)

    minute_buckets: Counter = Counter()
    for respuesta in respuestas:
        submitted_at = respuesta.submitted_at
        if not submitted_at:
            continue
        dt = submitted_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        minute_buckets[dt] += 1

    window = max(5, min(int(window_minutes or 10), 60))
    horizon = max(15, min(int(horizon_minutes or 60), 240))

    recent_values: List[int] = []
    for offset in range(window):
        bucket = (now - timedelta(minutes=offset)).replace(second=0, microsecond=0)
        recent_values.append(int(minute_buckets.get(bucket, 0)))

    moving_avg = round(sum(recent_values) / len(recent_values), 3) if recent_values else 0.0
    projected_additional = int(round(moving_avg * horizon))
    projected_total = len(respuestas) + projected_additional

    confidence = "media"
    if len(respuestas) < 20:
        confidence = "baja"
    elif len(respuestas) > 200:
        confidence = "alta"

    return {
        "encuesta_id": encuesta.id,
        "window_minutes": window,
        "horizon_minutes": horizon,
        "baseline_total": len(respuestas),
        "current_rate_per_minute": moving_avg,
        "projected_additional": projected_additional,
        "projected_total": projected_total,
        "confidence": confidence,
        "updated_at": now.isoformat(),
    }


def get_alerts(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    window_minutes: int = 10,
    min_activity_threshold: int = 5,
) -> Dict[str, Any]:
    """Evaluate alert rules for campaign operations dashboards."""

    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)

    window = max(5, min(int(window_minutes or 10), 30))
    now = datetime.now(timezone.utc)
    last_window = 0
    previous_window = 0
    for respuesta in respuestas:
        submitted_at = respuesta.submitted_at
        if not submitted_at:
            continue
        delta_seconds = (now - submitted_at.astimezone(timezone.utc)).total_seconds()
        if delta_seconds <= window * 60:
            last_window += 1
        elif delta_seconds <= window * 120:
            previous_window += 1

    delta = last_window - previous_window
    trend = "estable"
    if delta > 0:
        trend = "subiendo"
    elif delta < 0:
        trend = "bajando"

    alerts: List[Dict[str, Any]] = []
    if trend == "bajando" and previous_window >= min_activity_threshold:
        alerts.append(
            {
                "code": "participacion_en_caida",
                "severity": "high" if delta <= -max(3, min_activity_threshold // 2) else "medium",
                "message": "La participación cayó en la ventana reciente. Recomendada activación de recordatorios.",
                "delta": delta,
            }
        )
    if trend == "subiendo" and last_window >= min_activity_threshold:
        alerts.append(
            {
                "code": "momento_favorable",
                "severity": "info",
                "message": "La participación está acelerando. Buen momento para ampliar difusión.",
                "delta": delta,
            }
        )

    summary = get_summary(encuesta_id, filtros)
    leader_payload = None
    if summary.get("preguntas"):
        candidate_options: List[Dict[str, Any]] = []
        for pregunta in summary["preguntas"]:
            opciones = pregunta.get("opciones") or []
            if opciones:
                sorted_options = sorted(opciones, key=lambda item: item.get("porcentaje", 0), reverse=True)
                candidate_options.append(sorted_options[0])
        if candidate_options:
            leader_payload = sorted(candidate_options, key=lambda item: item.get("porcentaje", 0), reverse=True)[0]
    if leader_payload and float(leader_payload.get("porcentaje") or 0) >= 60:
        alerts.append(
            {
                "code": "liderazgo_marcado",
                "severity": "info",
                "message": "Se detecta un liderazgo fuerte en una opción de respuesta.",
                "value": leader_payload.get("porcentaje"),
            }
        )

    return {
        "encuesta_id": encuesta.id,
        "window_minutes": max(5, min(int(window_minutes or 10), 30)),
        "threshold": max(1, int(min_activity_threshold or 5)),
        "alerts": alerts,
        "has_alerts": bool(alerts),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_executive_brief(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return an executive-ready summary object for frontend reporting."""

    encuesta = get_encuesta(encuesta_id)
    summary = get_summary(encuesta_id, filtros)
    forecast = get_forecast(encuesta_id, filtros=filtros)
    alerts = get_alerts(encuesta_id, filtros=filtros)

    headline = (
        f"{summary['total_respuestas']} respuestas totales con proyección a "
        f"{forecast['projected_total']} en {forecast['horizon_minutes']} minutos."
    )

    return {
        "encuesta_id": encuesta.id,
        "titulo": encuesta.titulo,
        "headline": headline,
        "summary": {
            "total_respuestas": summary.get("total_respuestas", 0),
            "participantes_unicos": summary.get("participantes_unicos", 0),
            "tasa_completitud": summary.get("tasa_completitud", 0),
        },
        "forecast": forecast,
        "alerts": alerts,
        "insights": [
            headline,
            "Monitorear delta de momentum para decisiones tácticas de difusión.",
        ],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }



def _matches_segment(respuesta: EncRespuesta, segment: Optional[Dict[str, Any]]) -> bool:
    if not segment:
        return True
    for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais"):
        expected = segment.get(key)
        if expected is None or expected == "":
            continue
        value = getattr(respuesta, key, None)
        if str(value or "").strip().lower() != str(expected).strip().lower():
            return False
    return True


def _segment_distribution(
    respuestas: Sequence[EncRespuesta],
    encuesta: EncEncuesta,
) -> Dict[str, Any]:
    question_payload: List[Dict[str, Any]] = []
    for pregunta in encuesta.preguntas:
        normalized_tipo = _normalize_question_type(pregunta.tipo)
        if normalized_tipo not in {"single_choice", "multiple_choice"}:
            continue

        option_counter: Counter = Counter()
        for respuesta in respuestas:
            for detalle in respuesta.detalles:
                if detalle.pregunta_id != pregunta.id or not detalle.opcion_id:
                    continue
                option_counter[detalle.opcion_id] += 1

        options = []
        total_votes = sum(option_counter.values())
        for opcion in pregunta.opciones:
            votos = int(option_counter.get(opcion.id, 0))
            pct = round((votos / total_votes * 100), 2) if total_votes else 0.0
            options.append({
                "opcion_id": opcion.id,
                "label": opcion.texto,
                "votos": votos,
                "porcentaje": pct,
            })
        options.sort(key=lambda item: item["votos"], reverse=True)
        question_payload.append(
            {
                "pregunta_id": pregunta.id,
                "texto": pregunta.texto,
                "tipo": normalized_tipo,
                "total_votos": total_votes,
                "opciones": options,
            }
        )

    canales = Counter((respuesta.canal or "sin_canal") for respuesta in respuestas)
    return {
        "total_respuestas": len(respuestas),
        "canales": [{"label": k, "value": v} for k, v in canales.most_common()],
        "preguntas": question_payload,
    }




def _build_executive_summary_text(
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
    heatmap: Dict[str, Any],
) -> Dict[str, Any]:
    """Compose concise executive-ready narrative from analytics signals."""

    total = int(summary.get("total_respuestas") or 0)
    completion = float(summary.get("tasa_completitud") or 0)
    projected_total = int(forecast.get("projected_total") or total)
    projected_additional = int(forecast.get("projected_additional") or 0)
    alerts_list = alerts.get("alerts") or []

    top_barrio = None
    territorio = (summary.get("demografia") or {}).get("territorio_map") or {}
    barrios = territorio.get("barrios") if isinstance(territorio, dict) else None
    if isinstance(barrios, list) and barrios:
        top_barrio = barrios[0].get("label")

    top_hotspot = None
    map_hotspots = ((heatmap.get("metadata") or {}).get("map") or {}).get("hotspots")
    if isinstance(map_hotspots, list) and map_hotspots:
        top_hotspot = map_hotspots[0].get("label")

    priorities = []
    if completion < 65:
        priorities.append("Subir completitud: revisar fricción del formulario y recordar cierre de encuesta.")
    if projected_additional <= 0:
        priorities.append("Activar difusión táctica en canales de mayor conversión para recuperar ritmo.")
    if alerts_list:
        priorities.append("Atender alertas operativas detectadas antes de escalar pauta/publicidad.")
    if top_hotspot or top_barrio:
        priorities.append(
            f"Enfocar acciones territoriales en {top_hotspot or top_barrio} y replicar aprendizaje en zonas similares."
        )

    if not priorities:
        priorities.append("Mantener estrategia actual y escalar en los canales con mejor desempeño.")

    headline = (
        f"{total} respuestas registradas ({completion:.1f}% de completitud) "
        f"con proyección a {projected_total} en la ventana actual."
    )

    return {
        "headline": headline,
        "one_liner": (
            "La operación está en curso con señales accionables para priorizar territorio, "
            "canales y experiencia de respuesta."
        ),
        "focus_points": priorities[:4],
        "alert_count": len(alerts_list),
        "projected_additional": projected_additional,
    }


def _build_visual_blueprint(
    *,
    encuesta_id: int,
    summary: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    heatmap: Dict[str, Any],
) -> Dict[str, Any]:
    """Return chart/table specs so frontend can render a premium dashboard quickly."""

    preguntas = summary.get("preguntas") or []
    ranked_questions = sorted(
        [p for p in preguntas if isinstance(p, dict)],
        key=lambda item: int(item.get("total_respuestas") or 0),
        reverse=True,
    )

    top_questions = [
        {
            "pregunta_id": q.get("pregunta_id"),
            "texto": q.get("texto"),
            "tipo": q.get("tipo"),
            "series": q.get("series") or [],
        }
        for q in ranked_questions[:5]
    ]

    heatmap_meta = (heatmap.get("metadata") or {}).get("map") or {}
    hotspot_data = heatmap_meta.get("hotspots") if isinstance(heatmap_meta, dict) else []

    return {
        "charts": [
            {
                "id": "activity_timeseries",
                "type": "line",
                "title": "Evolución temporal de respuestas",
                "dataset_key": "timeseries",
                "x": "fecha",
                "y": "total",
            },
            {
                "id": "territory_heatmap",
                "type": "geospatial_heatmap",
                "title": "Intensidad territorial",
                "dataset_key": "heatmap.points",
                "layer_key": "heatmap_layer",
            },
            {
                "id": "hotspots_rank",
                "type": "bar",
                "title": "Top zonas calientes",
                "dataset_key": "heatmap.hotspots",
                "x": "label",
                "y": "weight",
            },
            {
                "id": "demography_gender",
                "type": "donut",
                "title": "Distribución por género",
                "dataset_key": "summary.demografia.genero",
                "x": "label",
                "y": "value",
            },
        ],
        "tables": [
            {
                "id": "questions_priority",
                "title": "Preguntas con mayor volumen",
                "dataset_key": "summary.top_questions",
                "columns": ["pregunta_id", "texto", "tipo"],
            },
            {
                "id": "hotspot_table",
                "title": "Detalle de hotspots",
                "dataset_key": "heatmap.hotspots",
                "columns": ["rank", "label", "weight", "intensity"],
            },
        ],
        "datasets": {
            "summary": summary,
            "timeseries": list(timeseries),
            "heatmap": {
                "points": heatmap.get("points") or [],
                "cells": heatmap.get("cells") or [],
                "hotspots": hotspot_data if isinstance(hotspot_data, list) else [],
            },
            "top_questions": top_questions,
        },
        "frontend_contract": {
            "version": "2026.02",
            "auth_demo": {
                "catalog_endpoint": "/auth/demo/catalog",
                "login_endpoint": "/auth/demo",
                "quick_login_field": "quick_login_payload.tenant_slug",
            },
            "map": {
                "preferred_provider": ((heatmap.get("metadata") or {}).get("map_config") or {}).get("provider") or "maplibre",
                "required_fields": ["lat", "lng", "weight"],
                "optional_layers": ["heatmap", "cells", "pulses", "hotspots"],
            },
        },
    }




def _metric_number(value: Any, *, decimals: int = 2) -> float:
    """Normalize numeric KPI values for UI payloads."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(numeric, decimals)


def _build_dashboard_cards(summary: Dict[str, Any], forecast: Dict[str, Any], anomalies: Dict[str, Any]) -> List[Dict[str, Any]]:
    total = int(summary.get("total_respuestas") or 0)
    participantes = int(summary.get("participantes_unicos") or 0)
    completitud = _metric_number(summary.get("tasa_completitud"), decimals=1)
    projected = int(forecast.get("projected_total") or total)
    risk_score = _metric_number(anomalies.get("risk_score"), decimals=3)

    return [
        {"id": "total_respuestas", "label": "Total de respuestas", "value": total, "unit": "count", "kind": "kpi"},
        {"id": "participantes_unicos", "label": "Participantes únicos", "value": participantes, "unit": "count", "kind": "kpi"},
        {"id": "tasa_completitud", "label": "Tasa de completitud", "value": completitud, "unit": "percent", "kind": "kpi"},
        {"id": "projected_total", "label": "Proyección total", "value": projected, "unit": "count", "kind": "forecast"},
        {"id": "risk_score", "label": "Riesgo operativo", "value": risk_score, "unit": "score", "kind": "anomaly"},
    ]


def _build_dashboard_ui_state(summary: Dict[str, Any], heatmap: Dict[str, Any], alerts: Dict[str, Any]) -> Dict[str, Any]:
    total_respuestas = int(summary.get("total_respuestas") or 0)
    points = heatmap.get("points") or []
    has_points = isinstance(points, list) and len(points) > 0
    has_alerts = bool((alerts.get("alerts") or []))

    return {
        "latest_responses": "ready" if total_respuestas > 0 else "empty",
        "map_participation": "ready" if has_points else "empty",
        "alerts": "attention" if has_alerts else "normal",
    }
def get_dashboard_bundle(
    encuesta_id: int,
    filtros: Optional[Dict[str, Any]] = None,
    *,
    granularity: str = "day",
) -> Dict[str, Any]:
    """Return a complete analytics payload optimized for executive dashboards."""

    summary = get_summary(encuesta_id, filtros)
    timeseries = get_timeseries(encuesta_id, granularity, filtros)
    heatmap = get_heatmap(encuesta_id, filtros)
    forecast = get_forecast(encuesta_id, filtros=filtros)
    alerts = get_alerts(encuesta_id, filtros=filtros)
    brief = get_executive_brief(encuesta_id, filtros)
    anomalies = get_anomaly_report(encuesta_id, filtros=filtros)

    executive_summary = _build_executive_summary_text(summary, forecast, alerts, heatmap)
    visual_blueprint = _build_visual_blueprint(
        encuesta_id=encuesta_id,
        summary=summary,
        timeseries=timeseries,
        heatmap=heatmap,
    )

    cards = _build_dashboard_cards(summary, forecast, anomalies)
    ui_state = _build_dashboard_ui_state(summary, heatmap, alerts)
    active_alerts = int(len(alerts.get("alerts") or []))

    return {
        "encuesta_id": encuesta_id,
        "executive_summary": executive_summary,
        "brief": brief,
        "kpis": {
            "total_respuestas": int(summary.get("total_respuestas") or 0),
            "participantes_unicos": int(summary.get("participantes_unicos") or 0),
            "tasa_completitud": _metric_number(summary.get("tasa_completitud"), decimals=2),
            "projected_total": int(forecast.get("projected_total") or 0),
            "risk_score": _metric_number(anomalies.get("risk_score"), decimals=3),
            "active_alerts": active_alerts,
        },
        "cards": cards,
        "ui_state": ui_state,
        "meta": {
            "schema_version": "2026.03",
            "filters": dict(filtros or {}),
            "module_state": {
                "summary": "ready" if int(summary.get("total_respuestas") or 0) > 0 else "empty",
                "timeseries": "ready" if len(timeseries or []) > 0 else "empty",
                "heatmap": "ready" if len((heatmap.get("points") or [])) > 0 else "empty",
                "alerts": "attention" if active_alerts > 0 else "normal",
            },
        },
        "modules": {
            "summary": summary,
            "timeseries": timeseries,
            "heatmap": heatmap,
            "forecast": forecast,
            "alerts": alerts,
            "anomalies": anomalies,
        },
        "visual_blueprint": visual_blueprint,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_segment_compare(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    segment_a: Optional[Dict[str, Any]] = None,
    segment_b: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)

    group_a = [respuesta for respuesta in respuestas if _matches_segment(respuesta, segment_a)]
    group_b = [respuesta for respuesta in respuestas if _matches_segment(respuesta, segment_b)]

    return {
        "encuesta_id": encuesta.id,
        "segment_a": {
            "filters": segment_a or {},
            "stats": _segment_distribution(group_a, encuesta),
        },
        "segment_b": {
            "filters": segment_b or {},
            "stats": _segment_distribution(group_b, encuesta),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_anomaly_report(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    burst_window_minutes: int = 5,
    burst_threshold: int = 10,
) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)

    ip_counter = Counter((respuesta.ip or "") for respuesta in respuestas if respuesta.ip)
    fingerprint_counter = Counter(
        (respuesta.huella_unica or "") for respuesta in respuestas if respuesta.huella_unica
    )
    geo_counter = Counter(
        (round(float(respuesta.lat), 3), round(float(respuesta.lng), 3))
        for respuesta in respuestas
        if respuesta.lat is not None and respuesta.lng is not None
    )

    window = max(1, min(int(burst_window_minutes or 5), 30))
    threshold = max(3, int(burst_threshold or 10))
    now = datetime.now(timezone.utc)
    burst_count = 0
    for respuesta in respuestas:
        if not respuesta.submitted_at:
            continue
        if (now - respuesta.submitted_at.astimezone(timezone.utc)).total_seconds() <= window * 60:
            burst_count += 1

    suspicious_ips = [
        {"ip": ip, "count": count}
        for ip, count in ip_counter.most_common(5)
        if count >= 3
    ]
    repeated_fingerprints = [
        {"fingerprint": fp, "count": count}
        for fp, count in fingerprint_counter.most_common(5)
        if count >= 2
    ]
    concentrated_geo = [
        {"lat": lat, "lng": lng, "count": count}
        for (lat, lng), count in geo_counter.most_common(5)
        if count >= 3
    ]

    score = 0
    score += min(len(suspicious_ips) * 12, 36)
    score += min(len(repeated_fingerprints) * 15, 30)
    score += min(len(concentrated_geo) * 10, 20)
    if burst_count >= threshold:
        score += 20
    score = min(score, 100)

    risk_level = "bajo"
    if score >= 65:
        risk_level = "alto"
    elif score >= 35:
        risk_level = "medio"

    return {
        "encuesta_id": encuesta.id,
        "risk_score": score,
        "risk_level": risk_level,
        "burst_window_minutes": window,
        "burst_threshold": threshold,
        "burst_count": burst_count,
        "signals": {
            "suspicious_ips": suspicious_ips,
            "repeated_fingerprints": repeated_fingerprints,
            "concentrated_geo": concentrated_geo,
        },
        "updated_at": now.isoformat(),
    }

def get_heatmap(
    encuesta_id: int,
    filtros: Optional[Dict[str, Any]] = None,
    *,
    resolution: Optional[int] = None,
) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    points, cells = _aggregate_heatmap_cells(respuestas, resolution=resolution)
    used_synthetic_points = False
    if not points and respuestas:
        synthetic_points = _build_synthetic_heatmap_points(encuesta, respuestas)
        if synthetic_points:
            points = synthetic_points
            used_synthetic_points = True
    metadata = _build_heatmap_metadata(encuesta, points)
    map_filter = _build_map_filter(points)
    metadata.update(
        {
            "resolution": resolution or DEFAULT_HEATMAP_RESOLUTION,
            "unique_cells": len(cells),
            "has_coordinates": bool(points),
            "using_synthetic_points": used_synthetic_points,
        }
    )
    points_geojson = build_feature_collection(points)
    cells_geojson = build_feature_collection(cells)
    if points_geojson:
        metadata["points_geojson"] = points_geojson
    if cells_geojson:
        metadata["cells_geojson"] = cells_geojson

    map_config = get_map_config()
    if map_config:
        metadata["map_config"] = map_config
    supported_formats = ["points"]
    preferred_format = "points"
    if points_geojson:
        supported_formats.append("geojson")
        preferred_format = "geojson"
    provider_hint = map_config.get("provider") if isinstance(map_config, dict) else None
    if not provider_hint or provider_hint == "none":
        provider_hint = "maplibre"
    heatmap_layer = {
        "kind": "heatmap",
        "supported_formats": supported_formats,
        "preferred_format": preferred_format,
        "provider_hint": provider_hint,
        "supports_filters": bool(map_filter["keys"]),
        "filter_keys": map_filter["keys"],
        "source_keys": {"points": "points", "geojson": "points_geojson"},
    }
    metadata["heatmap_layer"] = heatmap_layer
    metadata["map_layers"] = {"heatmap": heatmap_layer}
    metadata["map_filter"] = map_filter
    return {"points": points, "cells": cells, "metadata": metadata}


def _mask_ip(ip: Optional[str]) -> str:
    if not ip:
        return ""
    if ":" in ip:  # IPv6
        parts = ip.split(":")
        if len(parts) > 4:
            parts = parts[:4] + ["****"]
        return ":".join(parts)
    parts = ip.split(".")
    if len(parts) == 4:
        parts[-1] = "***"
        return ".".join(parts)
    return ip


def export_csv(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Iterable[str]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)

    preguntas = encuesta.preguntas
    fieldnames = [
        "respuesta_id",
        "submitted_at",
        "canal",
        "utm_source",
        "utm_campaign",
        "genero",
        "rango_etario",
        "edad",
        "anio_nacimiento",
        "barrio",
        "ciudad",
        "provincia",
        "pais",
        "ip",
        "lat",
        "lng",
    ]
    for pregunta in preguntas:
        fieldnames.append(f"pregunta_{pregunta.id}")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for respuesta in respuestas:
        row = {
            "respuesta_id": respuesta.id,
            "submitted_at": respuesta.submitted_at.isoformat() if respuesta.submitted_at else "",
            "canal": respuesta.canal,
            "utm_source": respuesta.utm_source,
            "utm_campaign": respuesta.utm_campaign,
            "genero": respuesta.genero,
            "rango_etario": respuesta.rango_etario,
            "edad": respuesta.edad,
            "anio_nacimiento": respuesta.anio_nacimiento,
            "barrio": respuesta.barrio,
            "ciudad": respuesta.ciudad,
            "provincia": respuesta.provincia,
            "pais": respuesta.pais,
            "ip": _mask_ip(respuesta.ip),
            "lat": respuesta.lat,
            "lng": respuesta.lng,
        }
        detalles_por_pregunta = defaultdict(list)
        for detalle in respuesta.detalles:
            if detalle.opcion:
                detalles_por_pregunta[detalle.pregunta_id].append(detalle.opcion.texto)
            if detalle.texto_libre:
                detalles_por_pregunta[detalle.pregunta_id].append(detalle.texto_libre)
        for pregunta in preguntas:
            value = " | ".join(detalles_por_pregunta.get(pregunta.id, []))
            row[f"pregunta_{pregunta.id}"] = value
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

def calculate_live_results(
    slug_publico: str,
    *,
    include_heatmap: bool = True,
    max_points: int = 2000,
    max_cells: int = 200,
    momentum_window_minutes: int = 10,
) -> Dict[str, Any]:
    """
    Returns simplified aggregate counts for live voting animations.
    Optimized for frequent polling.
    """
    encuesta = get_public_encuesta(slug_publico)
    responses_count = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count()

    option_counts = {
        (pregunta_id, opcion_id): total
        for pregunta_id, opcion_id, total in (
            db.session.query(
                EncRespuestaDetalle.pregunta_id,
                EncRespuestaDetalle.opcion_id,
                db.func.count(EncRespuestaDetalle.id),
            )
            .join(EncRespuesta, EncRespuesta.id == EncRespuestaDetalle.respuesta_id)
            .filter(
                EncRespuesta.encuesta_id == encuesta.id,
                EncRespuestaDetalle.opcion_id.isnot(None),
            )
            .group_by(EncRespuestaDetalle.pregunta_id, EncRespuestaDetalle.opcion_id)
            .all()
        )
    }

    preguntas: List[Dict[str, Any]] = []
    highlights: List[str] = []
    for pregunta in encuesta.preguntas:
        normalized_type = _normalize_question_type(pregunta.tipo)
        if normalized_type not in {"single_choice", "multiple_choice"}:
            continue

        total_pregunta = 0
        opciones_payload: List[Dict[str, Any]] = []
        for opcion in pregunta.opciones:
            votos = int(option_counts.get((pregunta.id, opcion.id), 0) or 0)
            total_pregunta += votos
            opciones_payload.append(
                {
                    "id": opcion.id,
                    "label": opcion.texto,
                    "value": votos,
                    "votos": votos,
                }
            )

        opciones_payload.sort(key=lambda item: item["value"], reverse=True)
        for opcion_data in opciones_payload:
            porcentaje = (opcion_data["value"] / total_pregunta * 100) if total_pregunta else 0.0
            opcion_data["porcentaje"] = round(porcentaje, 2)

        lider = opciones_payload[0] if opciones_payload else None
        if lider and lider["value"] > 0:
            highlights.append(
                f"{pregunta.texto[:70]}: lidera '{lider['label']}' con {lider['porcentaje']}%."
            )

        preguntas.append(
            {
                "id": pregunta.id,
                "titulo": pregunta.texto,
                "tipo": normalized_type,
                "opciones": opciones_payload,
                "total_votos": total_pregunta,
                "is_multi": normalized_type == "multiple_choice",
            }
        )

    now = datetime.now(timezone.utc)
    window = max(5, min(momentum_window_minutes, 30))
    last_hour = now.timestamp() - 3600
    recent_responses = (
        EncRespuesta.query.with_entities(EncRespuesta.submitted_at)
        .filter(EncRespuesta.encuesta_id == encuesta.id)
        .order_by(EncRespuesta.submitted_at.desc())
        .limit(1000)
        .all()
    )
    bucket_counts: Counter = Counter()
    last_10m = 0
    previous_10m = 0
    for (submitted_at,) in recent_responses:
        if not submitted_at:
            continue
        dt = submitted_at.astimezone(timezone.utc)
        ts = dt.timestamp()
        if ts < last_hour:
            continue
        minute_bucket = dt.replace(second=0, microsecond=0)
        bucket_counts[minute_bucket] += 1

        delta_seconds = (now - dt).total_seconds()
        if delta_seconds <= window * 60:
            last_10m += 1
        elif delta_seconds <= window * 120:
            previous_10m += 1

    trend = "estable"
    if last_10m > previous_10m:
        trend = "subiendo"
    elif last_10m < previous_10m:
        trend = "bajando"

    timeline = [
        {"timestamp": bucket.isoformat(), "total": bucket_counts[bucket]}
        for bucket in sorted(bucket_counts.keys())
    ]

    points: List[Dict[str, Any]] = []
    cells: List[Dict[str, Any]] = []
    if include_heatmap:
        points, cells = _aggregate_heatmap_cells(
            _collect_respuestas(encuesta, filtros={"desde": (now.replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()}),
            resolution=9,
        )

    ai_summary = "Sin datos suficientes para resumen en vivo."
    if responses_count > 0:
        momentum_text = (
            "participación acelerando" if trend == "subiendo" else "participación estable" if trend == "estable" else "participación desacelerando"
        )
        top_highlights = " ".join(highlights[:2]) if highlights else "Todavía no hay liderazgo claro por opción."
        ai_summary = (
            f"{responses_count} respuestas registradas, con {momentum_text} en los últimos minutos. "
            f"{top_highlights}"
        )

    responses_last_hour = sum(bucket_counts.values())
    participation_per_minute = round(responses_last_hour / 60.0, 3) if responses_last_hour else 0.0
    top_question = None
    for pregunta in preguntas:
        if not pregunta.get("opciones"):
            continue
        top_option = pregunta["opciones"][0]
        if not top_question or top_option["value"] > top_question["lider"]["value"]:
            top_question = {
                "pregunta_id": pregunta["id"],
                "pregunta": pregunta["titulo"],
                "lider": top_option,
            }

    kpis = {
        "responses_last_hour": responses_last_hour,
        "participation_per_minute": participation_per_minute,
        "heatmap_coverage_cells": len(cells),
        "leader": top_question,
    }

    ai_insights: List[str] = []
    if top_question and top_question.get("lider"):
        ai_insights.append(
            f"La pregunta con mayor tracción es '{top_question['pregunta'][:70]}' y lidera '{top_question['lider']['label']}' con {top_question['lider']['porcentaje']}%."
        )
    if trend == "subiendo":
        ai_insights.append("La curva reciente de participación está acelerando: conviene reforzar distribución del link ahora.")
    elif trend == "bajando":
        ai_insights.append("La curva reciente está desacelerando: conviene activar recordatorios o pauta segmentada.")
    else:
        ai_insights.append("La curva reciente se mantiene estable: se sugiere sostener frecuencia de difusión.")

    return {
        "encuesta_id": encuesta.id,
        "slug": slug_publico,
        "total_respuestas": responses_count,
        "preguntas": preguntas,
        "timeline_minute": timeline,
        "momentum": {
            "window_minutes": window,
            "last_window": last_10m,
            "previous_window": previous_10m,
            "trend": trend,
            "delta": last_10m - previous_10m,
            "last_10m": last_10m,
            "previous_10m": previous_10m,
        },
        "kpis": kpis,
        "heatmap": {
            "enabled": include_heatmap,
            "points": points[:max_points],
            "cells": cells[:max_cells],
            "metadata": {
                "resolution": 9,
                "points_count": len(points),
                "cells_count": len(cells),
                "truncated_points": max(0, len(points) - max_points),
                "truncated_cells": max(0, len(cells) - max_cells),
            },
        },
        "ai_summary": ai_summary,
        "ai_insights": ai_insights,
        "updated_at": now.isoformat(),
    }
