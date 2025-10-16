"""Analytics helpers for surveys."""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import joinedload

from models import EncEncuesta, EncRespuesta
from services.encuestas_service import EncuestaError, get_encuesta, _parse_datetime


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
        pregunta_data = {
            "pregunta_id": pregunta.id,
            "texto": pregunta.texto,
            "tipo": pregunta.tipo,
            "total_respuestas": total,
        }
        if pregunta.tipo in {"opcion_unica", "opcion_multiple"}:
            opciones = []
            for opcion in pregunta.opciones:
                conteo = opciones_por_pregunta[pregunta.id][opcion.id]
                porcentaje = (conteo / total * 100) if total else 0
                opciones.append(
                    {
                        "opcion_id": opcion.id,
                        "texto": opcion.texto,
                        "conteo": conteo,
                        "porcentaje": round(porcentaje, 2),
                    }
                )
            pregunta_data["opciones"] = opciones
        else:
            muestras = textos_abiertos.get(pregunta.id, [])[:20]
            pregunta_data["muestras_texto"] = muestras
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

    demografia = {
        "genero": dict(generos),
        "rango_etario": dict(rangos_etarios),
        "edad": {
            "promedio": edad_promedio,
            "mediana": edad_mediana,
            "p90": edad_p90,
            "muestra": len(edades_ordenadas),
        },
        "territorio": {
            "barrios": _top_counter(barrios),
            "ciudades": _top_counter(ciudades),
            "provincias": _top_counter(provincias),
            "paises": _top_counter(paises),
        },
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


def get_heatmap(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Dict[str, List[Dict[str, float]]]:
    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    points: List[Dict[str, float]] = []
    for respuesta in respuestas:
        if respuesta.lat is None or respuesta.lng is None:
            continue
        points.append({"lat": float(respuesta.lat), "lng": float(respuesta.lng), "w": 1.0})
    return {"points": points}


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
