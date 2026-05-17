"""Analytics helpers for surveys."""
from __future__ import annotations

import csv
import io
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import joinedload

from database import db
from models import EncEncuesta, EncRespuesta, EncPregunta, EncRespuestaDetalle
from services.openai_bridge import client as openai_client
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



logger = logging.getLogger(__name__)
_MAP_CONTRACT_VERSION = "2026.04-maplibre-v1"
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


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "si"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _is_demo_respuesta(respuesta: EncRespuesta) -> bool:
    metadata = getattr(respuesta, "metadata_payload", None)
    if isinstance(metadata, dict):
        return bool(metadata.get("is_demo_seed") or metadata.get("demo") or metadata.get("synthetic"))
    return False


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
    respuestas = query.order_by(EncRespuesta.submitted_at.asc()).all()

    include_demo = True
    if filtros:
        if _as_bool(filtros.get("exclude_demo")) is True:
            include_demo = False
        parsed_include_demo = _as_bool(filtros.get("include_demo"))
        if parsed_include_demo is not None:
            include_demo = parsed_include_demo

    if include_demo:
        return respuestas

    return [respuesta for respuesta in respuestas if not _is_demo_respuesta(respuesta)]


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
                "categoria": _extract_response_category(respuesta),
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


def _extract_response_category(respuesta: EncRespuesta) -> str:
    """Return a category-like label for map segmentation from response details."""

    for detalle in (respuesta.detalles or []):
        opcion = getattr(detalle, "opcion", None)
        texto_opcion = (getattr(opcion, "texto", None) or "").strip() if opcion else ""
        if texto_opcion:
            return texto_opcion
        texto_libre = (getattr(detalle, "texto_libre", None) or "").strip()
        if texto_libre:
            return texto_libre[:80]
    return "sin_categoria"


def _build_category_heatmap_layers(points: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    palette = ["#EF4444", "#F97316", "#EAB308", "#22C55E", "#06B6D4", "#3B82F6", "#8B5CF6", "#EC4899"]
    grouped: Dict[str, Dict[str, Any]] = {}

    for point in points:
        if not isinstance(point, Mapping):
            continue
        lat = point.get("lat")
        lng = point.get("lng")
        if lat is None or lng is None:
            continue
        categoria = str(point.get("categoria") or "sin_categoria").strip().lower() or "sin_categoria"
        weight = float(point.get("weight") or point.get("w") or point.get("count") or 1.0)
        ts = point.get("ts") or point.get("submitted_at")
        bucket = grouped.setdefault(categoria, {"count": 0, "weight": 0.0, "points": []})
        bucket["count"] += 1
        bucket["weight"] += max(weight, 0.0)
        mapped_point = {"lat": float(lat), "lng": float(lng), "weight": round(max(weight, 0.0), 4)}
        if ts:
            mapped_point["ts"] = ts
        bucket["points"].append(mapped_point)

    ranked = sorted(grouped.items(), key=lambda item: item[1]["weight"], reverse=True)
    feature_collection: Dict[str, Any] = {"type": "FeatureCollection", "features": []}
    for name, data in ranked:
        for point in data.get("points") or []:
            feature_collection["features"].append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [point["lng"], point["lat"]]},
                    "properties": {
                        "categoria": name,
                        "weight": point.get("weight", 1.0),
                        "ts": point.get("ts"),
                    },
                }
            )

    map_config = get_map_config() or {}
    style_url = map_config.get("style_url") or "https://demotiles.maplibre.org/style.json"

    if not ranked:
        return {
            "provider": "maplibre",
            "engine": "maplibre-gl-js",
            "style_url": style_url,
            "contract_version": _MAP_CONTRACT_VERSION,
            "categories": [],
            "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": 0},
            "source": feature_collection,
            "source_options": {"cluster": True, "clusterMaxZoom": 14, "clusterRadius": 45},
            "layers": {
                "heatmap": {"id": "encuestas-heat", "type": "heatmap", "source": "encuestas"},
                "clusters": {"id": "encuestas-clusters", "type": "circle", "source": "encuestas"},
                "points": {"id": "encuestas-points", "type": "circle", "source": "encuestas"},
            },
            "interactions": {"hover": True, "time_slider": {"enabled": False, "field": "ts"}},
            "source_meta": {"total_input_points": len(points)},
            "telemetry": {
                "event_endpoint": "/api/analytics/event",
                "events": ["map_loaded", "layer_toggle", "time_slider_changed", "cluster_click"],
            },
        }

    max_weight = max(float(item[1]["weight"]) for item in ranked) or 1.0
    has_time_values = any(bool(point.get("ts")) for _, data in ranked for point in (data.get("points") or []))
    categories = []
    for index, (name, data) in enumerate(ranked):
        categories.append(
            {
                "categoria": name,
                "color": palette[index % len(palette)],
                "event_count": int(data["count"]),
                "total_weight": round(float(data["weight"]), 4),
                "intensity": round(float(data["weight"]) / max_weight, 4),
                "points": data["points"],
            }
        )

    return {
        "provider": "maplibre",
        "engine": "maplibre-gl-js",
        "style_url": style_url,
        "contract_version": _MAP_CONTRACT_VERSION,
        "source": feature_collection,
        "source_meta": {"total_input_points": len(points)},
        "source_options": {"cluster": True, "clusterRadius": 45, "clusterMaxZoom": 14},
        "layers": {
            "heatmap": {"id": "encuestas-heat", "type": "heatmap", "source": "encuestas"},
            "clusters": {"id": "encuestas-clusters", "type": "circle", "source": "encuestas", "filter": ["has", "point_count"]},
            "points": {"id": "encuestas-points", "type": "circle", "source": "encuestas", "filter": ["!", ["has", "point_count"]]},
        },
        "interactions": {"hover": True, "time_slider": {"enabled": bool(has_time_values), "field": "ts"}},
        "categories": categories,
        "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": round(max_weight, 4)},
        "telemetry": {
            "event_endpoint": "/api/analytics/event",
            "events": ["map_loaded", "layer_toggle", "time_slider_changed", "cluster_click"],
        },
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


def _generate_openai_executive_brief(
    encuesta: EncEncuesta,
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Optionally enrich executive brief with OpenAI when credentials are configured."""

    if not openai_client:
        return None

    payload = {
        "encuesta_id": encuesta.id,
        "titulo": encuesta.titulo,
        "summary": {
            "total_respuestas": summary.get("total_respuestas", 0),
            "participantes_unicos": summary.get("participantes_unicos", 0),
            "tasa_completitud": summary.get("tasa_completitud", 0),
        },
        "forecast": {
            "projected_total": forecast.get("projected_total", 0),
            "horizon_minutes": forecast.get("horizon_minutes", 0),
            "momentum": forecast.get("momentum", "stable"),
        },
        "alerts": alerts.get("alerts", []),
    }

    system_prompt = (
        "Eres un consultor senior de analítica cívica y experiencia ciudadana. "
        "Devuelve SOLO JSON con campos: headline (string <= 35 palabras), "
        "insights (array de 2 strings accionables), risk_level (low|medium|high)."
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        headline = str(parsed.get("headline") or "").strip()
        insights = parsed.get("insights") if isinstance(parsed.get("insights"), list) else []
        insights = [str(item).strip() for item in insights if str(item).strip()][:2]
        risk_level = str(parsed.get("risk_level") or "").strip().lower()

        if not headline:
            return None

        if risk_level not in {"low", "medium", "high"}:
            risk_level = "medium"

        return {"headline": headline, "insights": insights, "risk_level": risk_level}
    except Exception:
        logger.warning("[encuestas_analytics] OpenAI brief enrichment failed", exc_info=True)
        return None


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

    ai_brief = _generate_openai_executive_brief(encuesta, summary, forecast, alerts)
    final_headline = ai_brief.get("headline") if ai_brief else headline
    final_insights = (ai_brief.get("insights") if ai_brief else None) or [
        headline,
        "Monitorear delta de momentum para decisiones tácticas de difusión.",
    ]

    return {
        "encuesta_id": encuesta.id,
        "titulo": encuesta.titulo,
        "headline": final_headline,
        "summary": {
            "total_respuestas": summary.get("total_respuestas", 0),
            "participantes_unicos": summary.get("participantes_unicos", 0),
            "tasa_completitud": summary.get("tasa_completitud", 0),
        },
        "forecast": forecast,
        "alerts": alerts,
        "insights": final_insights,
        "ai_enhanced": bool(ai_brief),
        "risk_level": (ai_brief or {}).get("risk_level", "medium"),
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
        normalized_value = str(value or "").strip().lower()
        if isinstance(expected, (list, tuple, set)):
            candidates = {str(item or "").strip().lower() for item in expected if str(item or "").strip()}
            if normalized_value not in candidates:
                return False
            continue
        if "," in str(expected):
            candidates = {
                chunk.strip().lower()
                for chunk in str(expected).split(",")
                if chunk.strip()
            }
            if normalized_value not in candidates:
                return False
            continue
        if normalized_value != str(expected).strip().lower():
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


def get_segment_suggestions(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    """Return dynamic A/B segmentation suggestions from available survey data."""

    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    total = max(len(respuestas), 1)
    effective_limit = max(2, min(int(limit or 5), 10))

    suggestions: Dict[str, List[Dict[str, Any]]] = {}
    for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais"):
        counter = Counter(str(getattr(respuesta, key) or "").strip() for respuesta in respuestas)
        options: List[Dict[str, Any]] = []
        for label, count in counter.most_common(effective_limit):
            if not label:
                continue
            coverage = round((count / total) * 100, 2)
            options.append(
                {
                    "label": label,
                    "filters": {key: label},
                    "count": count,
                    "coverage": coverage,
                }
            )
        if options:
            suggestions[key] = options

    return {
        "encuesta_id": encuesta.id,
        "total_respuestas": len(respuestas),
        "dimensions": suggestions,
        "updated_at": datetime.now(timezone.utc).isoformat(),
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


def _build_dashboard_ui_state(summary: Dict[str, Any], heatmap: Dict[str, Any], alerts: Dict[str, Any], *, latest_responses_state: Optional[str] = None) -> Dict[str, Any]:
    total_respuestas = int(summary.get("total_respuestas") or 0)
    points = heatmap.get("points") or []
    cells = heatmap.get("cells") or []
    has_points = isinstance(points, list) and len(points) > 0
    has_cells = isinstance(cells, list) and len(cells) > 0
    has_alerts = bool((alerts.get("alerts") or []))

    latest_state = latest_responses_state or ("ready" if total_respuestas > 0 else "empty")

    return {
        "latest_responses": latest_state,
        "map_participation": "ready" if (has_points or has_cells) else "empty",
        "alerts": "attention" if has_alerts else "normal",
    }


def _build_dashboard_sections(
    *,
    summary: Dict[str, Any],
    heatmap: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
    brief: Dict[str, Any],
) -> Dict[str, Any]:
    """Expose explicit sections for FE tabs (mapas/estadísticas/IA)."""

    demografia = summary.get("demografia") or {}
    territorio = demografia.get("territorio") or []
    map_meta = (heatmap.get("metadata") or {}).get("map") or {}

    return {
        "mapas": {
            "heatmap": {
                "points": heatmap.get("points") or [],
                "cells": heatmap.get("cells") or [],
                "hotspots": map_meta.get("hotspots") or [],
                "category_layers": ((heatmap.get("metadata") or {}).get("category_layers") or {}),
                "provider_hint": ((heatmap.get("metadata") or {}).get("map_config") or {}).get("provider") or "maplibre",
                "state": "ready" if bool((heatmap.get("points") or []) or (heatmap.get("cells") or [])) else "empty",
            },
            "territorio": territorio,
        },
        "estadisticas": {
            "resumen": {
                "total_respuestas": int(summary.get("total_respuestas") or 0),
                "participantes_unicos": int(summary.get("participantes_unicos") or 0),
                "tasa_completitud": _metric_number(summary.get("tasa_completitud"), decimals=2),
                "projected_total": int(forecast.get("projected_total") or 0),
            },
            "categorias": _build_category_rankings(summary),
            "demografia": {
                "genero": demografia.get("genero") or [],
                "rango_etario": demografia.get("rango_etario") or [],
                "edad": demografia.get("edad") or {},
            },
            "canales": summary.get("canales") or [],
            "series": list(timeseries or []),
        },
        "ia": {
            "headline": brief.get("headline") or "",
            "insights": brief.get("insights") or [],
            "risk_level": brief.get("risk_level") or "medium",
            "ai_enhanced": bool(brief.get("ai_enhanced")),
            "alerts": alerts.get("alerts") or [],
        },
    }


def _build_frontend_render_contract(
    *,
    heatmap: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    latest_responses_state: str,
    fast_mode: bool,
) -> Dict[str, Any]:
    """Return explicit FE orchestration hints to keep charts/maps stable.

    This contract is consumed by the analytics frontend so it can prioritize
    chart engines, apply deterministic fallbacks and avoid mounting maps/charts
    when required datasets are absent.
    """

    map_config = ((heatmap.get("metadata") or {}).get("map_config") or {})
    preferred_provider = map_config.get("provider") or "maplibre"
    if preferred_provider == "none":
        preferred_provider = "maplibre"

    map_ready = bool(heatmap.get("points") or heatmap.get("cells"))
    timeseries_ready = bool(timeseries)

    return {
        "version": "2026.04",
        "hierarchy": {
            "chart_engines": ["echarts", "recharts", "plotly"],
            "map_engines": [preferred_provider, "maplibre", "google"],
        },
        "modules": {
            "timeseries": {
                "state": "ready" if timeseries_ready else "empty",
                "dataset_key": "modules.timeseries",
            },
            "heatmap": {
                "state": "ready" if map_ready else "empty",
                "dataset_key": "modules.heatmap.points",
                "fallback_dataset_key": "modules.heatmap.cells",
                "preferred_provider": preferred_provider,
                "fallback_provider": "maplibre",
            },
            "latest_responses": {
                "state": latest_responses_state,
                "dataset_key": "modules.latest_responses",
            },
        },
        "render_strategy": "fast" if fast_mode else "full",
    }


def _build_latest_responses_preview(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Return a compact latest responses list to keep UI summary consistent."""

    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    ordered = sorted(
        respuestas,
        key=lambda item: item.submitted_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    preview: List[Dict[str, Any]] = []
    for respuesta in ordered[: max(1, min(int(limit or 10), 50))]:
        preview.append(
            {
                "id": respuesta.id,
                "submitted_at": (respuesta.submitted_at.astimezone(timezone.utc).isoformat() if respuesta.submitted_at else None),
                "canal": respuesta.canal,
                "genero": respuesta.genero,
                "rango_etario": respuesta.rango_etario,
                "lat": respuesta.lat,
                "lng": respuesta.lng,
            }
        )

    return preview


def _build_geo_rankings(points: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Aggregate heatmap points by geo dimensions for executive drilldowns."""

    counters: Dict[str, Counter] = {
        "barrio": Counter(),
        "ciudad": Counter(),
        "provincia": Counter(),
    }

    for point in points:
        if not isinstance(point, Mapping):
            continue
        for key in counters:
            value = point.get(key)
            if value is None:
                continue
            label = str(value).strip()
            if label:
                counters[key][label] += 1

    return {
        key: [{"label": label, "value": value} for label, value in counter.most_common(10)]
        for key, counter in counters.items()
    }


def _build_heatmap_territorial_aggregations(
    points: Sequence[Dict[str, Any]],
    *,
    total_respuestas: int,
    baseline_rate: float,
) -> Dict[str, List[Dict[str, Any]]]:
    """Aggregate territorial metrics for barrio/distrito(ciudad)/ciudad views."""

    dimensions = {
        "by_barrio": "barrio",
        "by_distrito": "ciudad",
        "by_ciudad": "ciudad",
    }
    payload: Dict[str, List[Dict[str, Any]]] = {}
    safe_total = max(total_respuestas, 1)
    safe_baseline = baseline_rate if baseline_rate > 0 else 1.0

    for output_key, attr_key in dimensions.items():
        grouped: Dict[str, Dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, Mapping):
                continue
            label = str(point.get(attr_key) or "").strip()
            if not label:
                continue
            item = grouped.setdefault(label, {"respuestas": 0, "lat_sum": 0.0, "lng_sum": 0.0})
            item["respuestas"] += 1
            item["lat_sum"] += float(point.get("lat") or 0.0)
            item["lng_sum"] += float(point.get("lng") or 0.0)

        rows: List[Dict[str, Any]] = []
        for label, item in sorted(grouped.items(), key=lambda x: x[1]["respuestas"], reverse=True)[:20]:
            respuestas = int(item["respuestas"])
            participacion = round((respuestas / safe_total) * 100, 2)
            tasa_crecimiento = round((respuestas / safe_baseline), 3)
            normalized_density = round(participacion / 100, 4)
            centroid_lat = round(item["lat_sum"] / respuestas, 6) if respuestas else None
            centroid_lng = round(item["lng_sum"] / respuestas, 6) if respuestas else None
            rows.append(
                {
                    "label": label,
                    "respuestas": respuestas,
                    "participacion": participacion,
                    "tasa_crecimiento": tasa_crecimiento,
                    "riesgo": "high" if participacion >= 30 else "medium" if participacion >= 15 else "low",
                    "normalized_density": normalized_density,
                    "centroid": {"lat": centroid_lat, "lng": centroid_lng},
                }
            )
        payload[output_key] = rows

    return payload


def _build_visual_module_contract(
    module_id: str,
    *,
    title: str,
    description: str,
    empty_state: str,
    units: str,
    decimals: int,
    sort: str,
    thresholds: Optional[Dict[str, Any]] = None,
    palette: Optional[List[str]] = None,
    min_width: int = 280,
    min_height: int = 220,
    aspect_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "id": module_id,
        "title": title,
        "description": description,
        "empty_state": empty_state,
        "units": units,
        "decimals": decimals,
        "sort": sort,
        "thresholds": thresholds or {},
        "palette": palette or ["#1D4ED8", "#2563EB", "#38BDF8"],
        "container": {
            "min_width": int(min_width),
            "min_height": int(min_height),
            "aspect_ratio": aspect_ratio,
        },
    }


def _build_category_rankings(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build category-like ranking from survey questions/options distribution."""

    category_counter: Counter = Counter()
    for pregunta in summary.get("preguntas") or []:
        for opcion in pregunta.get("opciones") or []:
            label = str(opcion.get("texto") or "").strip()
            value = int(opcion.get("conteo") or opcion.get("value") or 0)
            if label and value > 0:
                category_counter[label] += value

    return [{"label": label, "value": value} for label, value in category_counter.most_common(12)]


def _build_admin_decision_cards(
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
    geo_rankings: Dict[str, List[Dict[str, Any]]],
    category_rankings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return concise decision cards for municipality and business operators."""

    top_barrio = (geo_rankings.get("barrio") or [{}])[0]
    top_city = (geo_rankings.get("ciudad") or [{}])[0]
    top_category = (category_rankings or [{}])[0]
    active_alerts = len(alerts.get("alerts") or [])

    return [
        {
            "id": "territory_focus",
            "title": "Foco territorial",
            "priority": "high" if top_barrio.get("label") else "medium",
            "message": (
                f"{top_barrio.get('label')} concentra mayor actividad"
                if top_barrio.get("label")
                else "No hay suficientes datos geográficos para priorizar barrios"
            ),
            "evidence": {
                "barrio": top_barrio,
                "ciudad": top_city,
            },
        },
        {
            "id": "category_focus",
            "title": "Categoría con mayor demanda",
            "priority": "medium",
            "message": (
                f"{top_category.get('label')} lidera las respuestas"
                if top_category.get("label")
                else "No se detectó una categoría dominante"
            ),
            "evidence": {
                "category": top_category,
                "total_respuestas": int(summary.get("total_respuestas") or 0),
            },
        },
        {
            "id": "operational_pulse",
            "title": "Pulso operativo",
            "priority": "high" if active_alerts > 0 else "low",
            "message": (
                f"{active_alerts} alertas activas: revisar campañas y soporte"
                if active_alerts > 0
                else "Sin alertas críticas activas"
            ),
            "evidence": {
                "projected_total": int(forecast.get("projected_total") or 0),
                "alerts": active_alerts,
            },
        },
    ]


def _build_admin_analytics_template(
    summary: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    heatmap: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a frontend-friendly advanced layout contract for survey analytics."""

    points = heatmap.get("points") or []
    geo_rankings = _build_geo_rankings(points)
    category_rankings = _build_category_rankings(summary)
    age_distribution = ((summary.get("demografia") or {}).get("rango_etario") or [])
    total_respuestas = int(summary.get("total_respuestas") or 0)
    territorial_aggregations = _build_heatmap_territorial_aggregations(
        points,
        total_respuestas=total_respuestas,
        baseline_rate=float(forecast.get("current_rate_per_minute") or 0.0),
    )

    return {
        "layout_version": "2026.04",
        "tabs": [
            {"id": "overview", "label": "Resumen ejecutivo", "default": True},
            {"id": "territory", "label": "Mapa territorial"},
            {"id": "categories", "label": "Categorías"},
            {"id": "demography", "label": "Demografía"},
            {"id": "ai_copilot", "label": "Copiloto IA"},
        ],
        "chart_stack": {
            "recommended": ["echarts", "plotly", "maplibre"],
            "notes": "Usar ECharts para KPIs/series y MapLibre para heatmaps por barrio, distrito y ciudad.",
        },
        "datasets": {
            "geo_rankings": geo_rankings,
            "category_rankings": category_rankings,
            "age_distribution": age_distribution,
            "activity_timeseries": list(timeseries),
            "heatmap_points": points,
            **territorial_aggregations,
        },
        "decision_cards": _build_admin_decision_cards(
            summary=summary,
            forecast=forecast,
            alerts=alerts,
            geo_rankings=geo_rankings,
            category_rankings=category_rankings,
        ),
        "ux_guardrails": {
            "chart_container": {
                "default_min_width": 280,
                "default_min_height": 220,
                "mobile_min_height": 240,
                "render_when_visible": True,
                "require_non_zero_parent_size": True,
            },
            "responsive": {
                "mobile_breakpoint_px": 768,
                "card_gap_mobile": 12,
                "stack_cards_on_mobile": True,
            },
            "maps": {
                "prefer_interactive_providers": ["maplibre", "google"],
                "fallback_to_static_geo_table": True,
            },
            "telemetry": {
                "event_endpoint_preferred": "/api/analytics/event",
                "event_endpoint_legacy": "/analytics/event",
                "requires_tenant": True,
                "fallback_event_name": "frontend_analytics_event",
            },
            "widget": {
                "config_endpoint_preferred": "/api/public/widget-config",
                "config_endpoint_legacy": "/public/widget-config",
                "retry_recommended": True,
            },
        },
        "visual_modules": [
            _build_visual_module_contract(
                "kpi_total",
                title="Participación total",
                description="Respuestas acumuladas en el período filtrado.",
                empty_state="Sin respuestas todavía.",
                units="count",
                decimals=0,
                sort="desc",
            ),
            _build_visual_module_contract(
                "heatmap_territory",
                title="Mapa territorial",
                description="Concentración por barrio, distrito y ciudad.",
                empty_state="No hay geodatos para el rango actual.",
                units="density",
                decimals=4,
                sort="desc",
                thresholds={"low": 0.1, "medium": 0.2, "high": 0.3},
            ),
            _build_visual_module_contract(
                "anomalies",
                title="Anomalías operativas",
                description="Detección de patrones de riesgo y manipulación.",
                empty_state="No se detectaron anomalías relevantes.",
                units="score",
                decimals=2,
                sort="desc",
                thresholds={"medium": 35, "high": 65, "critical": 80},
                palette=["#16A34A", "#F59E0B", "#EF4444", "#991B1B"],
            ),
        ],
    }




def _build_executive_kpis(
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    anomalies: Dict[str, Any],
    heatmap: Dict[str, Any],
    segment_compare: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Return sales-ready KPI objects with trend/status/explanation."""

    total = int(summary.get("total_respuestas") or 0)
    unique_participants = int(summary.get("participantes_unicos") or 0)
    points = heatmap.get("points") or []
    unique_geo = len({(round(float(item.get("lat") or 0), 3), round(float(item.get("lng") or 0), 3)) for item in points if isinstance(item, Mapping)})
    territorial = round((unique_geo / max(len(points), 1)) * 100, 2) if points else 0.0

    segment_a_total = int((((segment_compare.get("segment_a") or {}).get("stats") or {}).get("total_respuestas") or 0))
    segment_b_total = int((((segment_compare.get("segment_b") or {}).get("stats") or {}).get("total_respuestas") or 0))
    brecha_segmento = abs(segment_a_total - segment_b_total)

    risk_score = _metric_number(anomalies.get("risk_score"), decimals=2)
    confidence_index = round(max(0.0, 100.0 - risk_score), 2)
    avg_response_time = 0.0
    if len(timeseries) >= 2:
        avg_response_time = round(1440 / max((sum(item.get("total", 0) for item in timeseries) / len(timeseries)), 1), 2)

    trend_7d = _metric_number(forecast.get("current_rate_per_minute"), decimals=3)
    trend_30d = _metric_number((forecast.get("projected_additional") or 0) / max(forecast.get("horizon_minutes") or 1, 1), decimals=3)

    return {
        "participacion_total": {"value": total, "trend": trend_7d, "status": "good" if total > 0 else "neutral", "explanation": "Respuestas acumuladas en el período seleccionado."},
        "representatividad_territorial": {"value": territorial, "trend": 0.0, "status": "good" if territorial >= 40 else "watch", "explanation": "Cobertura geográfica en base a puntos únicos relevados."},
        "brecha_segmento_max": {"value": brecha_segmento, "trend": 0.0, "status": "good" if brecha_segmento <= max(unique_participants * 0.2, 5) else "watch", "explanation": "Diferencia absoluta de participación entre segmentos A/B."},
        "indice_confianza_datos": {"value": confidence_index, "trend": 0.0, "status": "good" if confidence_index >= 70 else "risk", "explanation": "Índice inverso al riesgo de anomalías detectadas."},
        "tiempo_respuesta_medio": {"value": avg_response_time, "trend": 0.0, "status": "good" if avg_response_time <= 60 else "watch", "explanation": "Minutos promedio estimados entre bloques de respuestas."},
        "tendencia_7d": {"value": trend_7d, "trend": trend_7d, "status": "good" if trend_7d >= 0.05 else "neutral", "explanation": "Tasa actual de participación por minuto (proxy 7d)."},
        "tendencia_30d": {"value": trend_30d, "trend": trend_30d, "status": "good" if trend_30d >= 0.05 else "neutral", "explanation": "Proyección media por minuto para horizonte extendido (proxy 30d)."},
    }
def get_dashboard_bundle(
    encuesta_id: int,
    filtros: Optional[Dict[str, Any]] = None,
    *,
    granularity: str = "day",
    fast_mode: bool = False,
) -> Dict[str, Any]:
    """Return a complete analytics payload optimized for executive dashboards."""

    summary = get_summary(encuesta_id, filtros)
    timeseries = get_timeseries(encuesta_id, granularity, filtros)
    heatmap = get_heatmap(encuesta_id, filtros)
    forecast = get_forecast(encuesta_id, filtros=filtros)
    alerts = get_alerts(encuesta_id, filtros=filtros)
    brief = get_executive_brief(encuesta_id, filtros)
    anomalies = {
        "encuesta_id": encuesta_id,
        "risk_score": 0.0,
        "risk_level": "low",
        "severity": "low",
        "signals": {},
        "top_anomalies": [],
    }
    segment_compare_default = {
        "segment_a": {"stats": {"total_respuestas": 0}},
        "segment_b": {"stats": {"total_respuestas": 0}},
    }
    if not fast_mode:
        anomalies = get_anomaly_report(encuesta_id, filtros=filtros)
        segment_compare_default = get_segment_compare(
            encuesta_id,
            filtros=filtros,
            segment_a={"canal": "web"},
            segment_b={"canal": "whatsapp"},
        )

    executive_summary = _build_executive_summary_text(summary, forecast, alerts, heatmap)
    visual_blueprint = _build_visual_blueprint(
        encuesta_id=encuesta_id,
        summary=summary,
        timeseries=timeseries,
        heatmap=heatmap,
    )
    admin_template = _build_admin_analytics_template(
        summary=summary,
        timeseries=timeseries,
        heatmap=heatmap,
        forecast=forecast,
        alerts=alerts,
    )

    latest_responses: List[Dict[str, Any]] = []
    latest_responses_error: Optional[str] = None
    latest_responses_state = "empty"
    if fast_mode:
        latest_responses_state = "deferred"
    else:
        try:
            latest_responses = _build_latest_responses_preview(encuesta_id, filtros=filtros, limit=10)
            latest_responses_state = "ready" if len(latest_responses) > 0 else "empty"
        except Exception as exc:  # pragma: no cover - defensive guard for dashboard stability
            logger.exception("[encuestas.analytics] latest responses preview failed encuesta_id=%s", encuesta_id)
            latest_responses_error = str(exc)
            latest_responses_state = "degraded"

    cards = _build_dashboard_cards(summary, forecast, anomalies)
    ui_state = _build_dashboard_ui_state(summary, heatmap, alerts, latest_responses_state=latest_responses_state)
    ui_state["render_strategy"] = "fast" if fast_mode else "full"
    active_alerts = int(len(alerts.get("alerts") or []))
    executive_kpis = _build_executive_kpis(
        summary=summary,
        forecast=forecast,
        anomalies=anomalies,
        heatmap=heatmap,
        segment_compare=segment_compare_default,
        timeseries=timeseries,
    )
    frontend_render_contract = _build_frontend_render_contract(
        heatmap=heatmap,
        timeseries=timeseries,
        latest_responses_state=latest_responses_state,
        fast_mode=fast_mode,
    )
    sections = _build_dashboard_sections(
        summary=summary,
        heatmap=heatmap,
        timeseries=timeseries,
        forecast=forecast,
        alerts=alerts,
        brief=brief,
    )

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
        "kpis_executive": executive_kpis,
        "ui_state": ui_state,
        "meta": {
            "schema_version": "2026.03",
            "filters": dict(filtros or {}),
            "fast_mode": fast_mode,
            "module_state": {
                "summary": "ready" if int(summary.get("total_respuestas") or 0) > 0 else "empty",
                "timeseries": "ready" if len(timeseries or []) > 0 else "empty",
                "heatmap": "ready" if bool((heatmap.get("points") or []) or (heatmap.get("cells") or [])) else "empty",
                "alerts": "attention" if active_alerts > 0 else "normal",
                "latest_responses": latest_responses_state,
            },
        },
        "modules": {
            "summary": summary,
            "timeseries": timeseries,
            "heatmap": heatmap,
            "forecast": forecast,
            "alerts": alerts,
            "anomalies": anomalies,
            "latest_responses": latest_responses,
            "latest_responses_meta": {
                "state": latest_responses_state,
                "error": latest_responses_error,
            },
        },
        "visual_blueprint": visual_blueprint,
        "admin_template": admin_template,
        "frontend_render_contract": frontend_render_contract,
        "sections": sections,
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

    total_base = max(len(respuestas), 1)

    def _segment_meta(name: str, filters_payload: Optional[Dict[str, Any]], group: Sequence[EncRespuesta]) -> Dict[str, Any]:
        count = len(group)
        return {
            "name": name,
            "label": f"Segmento {name.upper()}",
            "filters": filters_payload or {},
            "count": count,
            "coverage": round((count / total_base) * 100, 2),
        }

    return {
        "encuesta_id": encuesta.id,
        "segment_a": {
            "meta": _segment_meta("a", segment_a, group_a),
            "filters": segment_a or {},
            "stats": _segment_distribution(group_a, encuesta),
        },
        "segment_b": {
            "meta": _segment_meta("b", segment_b, group_b),
            "filters": segment_b or {},
            "stats": _segment_distribution(group_b, encuesta),
        },
        "comparison_meta": {
            "base_total": len(respuestas),
            "gap_respuestas": len(group_a) - len(group_b),
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

    def _severity_from_score(value: int) -> str:
        if value >= 80:
            return "critical"
        if value >= 60:
            return "high"
        if value >= 35:
            return "medium"
        return "low"

    def _confidence_from_count(count: int) -> str:
        if count >= 10:
            return "high"
        if count >= 4:
            return "medium"
        return "low"

    anomaly_signals: List[Dict[str, Any]] = []
    for item in suspicious_ips:
        anomaly_signals.append(
            {
                "type": "suspicious_ip",
                "detail": f"IP con repetición inusual ({item['count']} respuestas)",
                "score": min(100, item["count"] * 10),
                "why_it_matters": "Puede indicar automatización o manipulación de votos.",
                "recommended_action": "Aplicar verificación adicional (captcha/validación humana).",
                "affected_segment": {"ip": item["ip"]},
                "confidence": _confidence_from_count(item["count"]),
                "severity": _severity_from_score(min(100, item["count"] * 10)),
                "timestamp": now.isoformat(),
            }
        )
    for item in repeated_fingerprints:
        anomaly_signals.append(
            {
                "type": "repeated_fingerprint",
                "detail": f"Huella repetida ({item['count']} veces)",
                "score": min(100, item["count"] * 12),
                "why_it_matters": "Puede reflejar cuentas duplicadas o abuso desde mismo dispositivo.",
                "recommended_action": "Revisar reglas de unicidad y limitar múltiples envíos.",
                "affected_segment": {"fingerprint": item["fingerprint"]},
                "confidence": _confidence_from_count(item["count"]),
                "severity": _severity_from_score(min(100, item["count"] * 12)),
                "timestamp": now.isoformat(),
            }
        )
    for item in concentrated_geo:
        anomaly_signals.append(
            {
                "type": "geo_concentration",
                "detail": f"Concentración geográfica alta ({item['count']} respuestas en un punto)",
                "score": min(100, item["count"] * 8),
                "why_it_matters": "Concentraciones extremas sesgan representatividad territorial.",
                "recommended_action": "Comparar con histórico y abrir revisión territorial.",
                "affected_segment": {"lat": item["lat"], "lng": item["lng"]},
                "confidence": _confidence_from_count(item["count"]),
                "severity": _severity_from_score(min(100, item["count"] * 8)),
                "timestamp": now.isoformat(),
            }
        )
    if burst_count >= threshold:
        anomaly_signals.append(
            {
                "type": "burst_activity",
                "detail": f"Pico abrupto de actividad ({burst_count} respuestas en {window} min)",
                "score": min(100, burst_count * 3),
                "why_it_matters": "Picos repentinos pueden requerir moderación y capacidad operativa.",
                "recommended_action": "Escalar monitoreo en tiempo real y revisar fuentes de tráfico.",
                "affected_segment": {"window_minutes": window},
                "confidence": _confidence_from_count(burst_count),
                "severity": _severity_from_score(min(100, burst_count * 3)),
                "timestamp": now.isoformat(),
            }
        )

    anomaly_signals.sort(key=lambda signal: signal.get("score", 0), reverse=True)

    return {
        "encuesta_id": encuesta.id,
        "risk_score": score,
        "risk_level": risk_level,
        "severity": _severity_from_score(score),
        "burst_window_minutes": window,
        "burst_threshold": threshold,
        "burst_count": burst_count,
        "signals": {
            "suspicious_ips": suspicious_ips,
            "repeated_fingerprints": repeated_fingerprints,
            "concentrated_geo": concentrated_geo,
        },
        "top_anomalies": anomaly_signals[:10],
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
    allow_synthetic = bool(_as_bool((filtros or {}).get("allow_synthetic_geo") or (filtros or {}).get("include_synthetic_geo")))
    used_synthetic_points = False
    if allow_synthetic and not points and respuestas:
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
            "can_render_heatmap": bool(points or cells),
            "empty_reason": None if points or cells else "no_real_geo_points",
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
    metadata["category_layers"] = _build_category_heatmap_layers(points)
    metadata["map_filter"] = map_filter
    render_contract = {
        "module": "heatmap",
        "state": "ready" if bool(points or cells) else "empty",
        "dataset_key": "points",
        "fallback_dataset_key": "cells",
        "source_keys": ["points", "cells", "metadata.map_layers.heatmap"],
        "chart_hierarchy": ["echarts", "recharts", "plotly"],
        "map_hierarchy": [provider_hint, "maplibre", "google"],
        "can_render_heatmap": bool(points or cells),
        "empty_reason": None if points or cells else "no_real_geo_points",
    }
    return {
        "points": points,
        "cells": cells,
        "metadata": metadata,
        "render_contract": render_contract,
    }


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
