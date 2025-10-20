"""Reusable analytics pipeline for municipal innovation projects."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd
from flask import current_app, has_app_context
import h3
from extensions import db
from models import MunicipioTicket
from utils.heatmap import enrich_heatmap_points
from utils.privacy import pseudoanonymize
from utils.time_utils import get_local_now


_CLOSED_STATES = {"cerrado", "resuelto", "completado", "closed"}


@dataclass(frozen=True)
class IncidentRecord:
    """Lightweight representation of an incident for analytics purposes."""

    id: int
    created_at: datetime
    estado: str | None
    categoria: str | None
    distrito: str | None
    latitud: float | None
    longitud: float | None
    ultima_actividad: datetime | None
    canal: str | None
    anon_id: str | None


def _normalize_state(value: Optional[str]) -> str:
    if not value:
        return "desconocido"
    text = value.strip().lower()
    return text or "desconocido"


def _normalize_label(value: Optional[str], default: str) -> str:
    if not value:
        return default
    text = value.strip()
    return text or default


def _is_closed(value: Optional[str]) -> bool:
    return _normalize_state(value) in _CLOSED_STATES


def _ticket_resolution_hours(record: IncidentRecord) -> Optional[float]:
    if not record.ultima_actividad or not record.created_at:
        return None
    if not _is_closed(record.estado):
        return None
    delta = record.ultima_actividad - record.created_at
    return round(delta.total_seconds() / 3600, 2)


def _record_from_ticket(ticket: MunicipioTicket) -> IncidentRecord:
    return IncidentRecord(
        id=int(ticket.id),
        created_at=ticket.fecha or get_local_now(),
        estado=ticket.estado,
        categoria=ticket.categoria,
        distrito=ticket.distrito,
        latitud=ticket.latitud,
        longitud=ticket.longitud,
        ultima_actividad=getattr(ticket, "ultima_actividad", None),
        canal=getattr(ticket, "canal_ingreso", None),
        anon_id=getattr(ticket, "anon_id", None),
    )


def load_incidents_for_municipio(
    municipio_id: int,
    *,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[IncidentRecord]:
    """Fetch incidents for the given municipality and convert them to records."""

    query = db.session.query(MunicipioTicket).filter(MunicipioTicket.municipio_id == municipio_id)
    if date_from is not None:
        query = query.filter(MunicipioTicket.fecha >= date_from)
    if date_to is not None:
        query = query.filter(MunicipioTicket.fecha < date_to)

    tickets: Sequence[MunicipioTicket] = query.order_by(MunicipioTicket.fecha.desc()).all()
    return [_record_from_ticket(ticket) for ticket in tickets]


def build_scorecards(records: Sequence[IncidentRecord]) -> Dict[str, object]:
    """Generate KPI scorecards covering incidents, SLA and resolution times."""

    totals = {
        "incidentes": len(records),
        "abiertos": 0,
        "cerrados": 0,
    }
    distritos: Counter[str] = Counter()
    categorias: Counter[str] = Counter()
    canales: Counter[str] = Counter()
    resoluciones: List[float] = []
    primeras_24h = 0

    for record in records:
        state = _normalize_state(record.estado)
        if _is_closed(state):
            totals["cerrados"] += 1
        else:
            totals["abiertos"] += 1

        distrito = _normalize_label(record.distrito, "Sin distrito")
        distritos[distrito] += 1

        categoria = _normalize_label(record.categoria, "Sin categoría")
        categorias[categoria] += 1

        canal = _normalize_label(record.canal, "Sin canal")
        canales[canal] += 1

        resolution_hours = _ticket_resolution_hours(record)
        if resolution_hours is not None:
            resoluciones.append(resolution_hours)
            if resolution_hours <= 24:
                primeras_24h += 1

    backlog = totals["abiertos"]
    sla_pct = round((primeras_24h * 100 / totals["cerrados"]) if totals["cerrados"] else 0.0, 2)

    tiempos = {
        "promedio_horas": round(mean(resoluciones), 2) if resoluciones else None,
        "mediana_horas": round(median(resoluciones), 2) if resoluciones else None,
        "p95_horas": round(float(np.percentile(resoluciones, 95)), 2) if resoluciones else None,
    }

    return {
        "totals": {**totals, "backlog": backlog, "sla_24h_pct": sla_pct},
        "incidentes_por_barrio": _counter_payload(distritos),
        "incidentes_por_categoria": _counter_payload(categorias),
        "incidentes_por_canal": _counter_payload(canales),
        "tiempos_resolucion": tiempos,
    }


def _counter_payload(counter: Counter[str]) -> List[Dict[str, object]]:
    return [
        {"label": label, "total": total}
        for label, total in counter.most_common()
    ]


def _deterministic_noise(key: str, amplitude: float = 0.15) -> float:
    digest = pseudoanonymize(key, salt="geo-noise") or "0"
    seed = int(digest, 16)
    # Convert the hash into a floating point number between -1 and 1.
    normalized = (seed % 1000) / 1000.0
    scaled = (normalized * 2) - 1
    return scaled * amplitude


def build_heatmap(
    records: Sequence[IncidentRecord],
    *,
    resolution: int = 7,
    min_count: int = 3,
) -> Dict[str, object]:
    """Aggregate incidents into H3 hexagons with privacy-preserving noise."""

    cells: Dict[str, Dict[str, object]] = {}
    for record in records:
        if record.latitud is None or record.longitud is None:
            continue
        try:
            cell_id = h3.latlng_to_cell(record.latitud, record.longitud, resolution)
        except Exception:
            if has_app_context():
                current_app.logger.debug("Invalid coordinates for ticket %s", record.id)
            continue

        cell = cells.setdefault(
            cell_id,
            {
                "cell_id": cell_id,
                "total": 0,
                "categorias": Counter(),
                "estados": Counter(),
            },
        )

        cell["total"] += 1
        cell["categorias"].update([_normalize_label(record.categoria, "Sin categoría")])
        cell["estados"].update([_normalize_state(record.estado)])

    features: List[Dict[str, object]] = []
    for cell_id, cell in cells.items():
        if cell["total"] < min_count:
            continue

        lat, lon = h3.cell_to_latlng(cell_id)
        noise_factor = 1 + _deterministic_noise(cell_id)
        adjusted = max(int(round(cell["total"] * noise_factor)), cell["total"])
        feature = {
            "cell_id": cell_id,
            "lat": round(lat, 6),
            "lng": round(lon, 6),
            "total": adjusted,
            "categorias": _counter_payload(cell["categorias"]),
            "estados": _counter_payload(cell["estados"]),
        }
        features.append(feature)

    enrich_heatmap_points(features, property_keys=("categorias", "estados"))
    return {"cells": features, "resolution": resolution, "min_count": min_count}


def demand_forecast(
    records: Sequence[IncidentRecord],
    *,
    periods: int = 14,
) -> Dict[str, object]:
    """Project incident demand with a simple trend-aware forecast."""

    if periods <= 0:
        periods = 1

    if not records:
        future_dates = [get_local_now().date() + timedelta(days=offset) for offset in range(1, periods + 1)]
        return {
            "historical": [],
            "forecast": [{"date": date.isoformat(), "expected": 0, "lower": 0, "upper": 0} for date in future_dates],
        }

    df = pd.DataFrame(
        {
            "fecha": [record.created_at.date() for record in records],
            "estado": [_normalize_state(record.estado) for record in records],
        }
    )
    counts = df.groupby("fecha").size().sort_index()
    index = np.arange(len(counts))
    slope = 0.0
    intercept = counts.iloc[-1] if not counts.empty else 0.0
    if len(counts) >= 2:
        slope, intercept = np.polyfit(index, counts.values, 1)

    historical = [
        {"date": date.isoformat(), "total": int(total)} for date, total in counts.items()
    ]

    last_index = index[-1] if len(index) else 0
    forecast_rows: List[Dict[str, object]] = []
    for offset in range(1, periods + 1):
        idx = last_index + offset
        expected = slope * idx + intercept
        expected = max(expected, 0)
        lower = max(expected * 0.8, 0)
        upper = expected * 1.2
        forecast_date = counts.index[-1] + timedelta(days=offset)
        forecast_rows.append(
            {
                "date": forecast_date.isoformat(),
                "expected": round(float(expected), 2),
                "lower": round(float(lower), 2),
                "upper": round(float(upper), 2),
            }
        )

    return {"historical": historical, "forecast": forecast_rows}


def cluster_incidents(
    records: Sequence[IncidentRecord],
    *,
    resolution: int = 8,
    min_count: int = 5,
) -> Dict[str, object]:
    """Cluster incidents using hierarchical H3 aggregation."""

    heatmap = build_heatmap(records, resolution=resolution, min_count=min_count)
    clusters: List[Dict[str, object]] = []
    for cell in heatmap["cells"]:
        cluster = {
            "cell_id": cell["cell_id"],
            "lat": cell["lat"],
            "lng": cell["lng"],
            "total": cell["total"],
            "top_categoria": cell.get("categorias", [{}])[0] if cell.get("categorias") else None,
        }
        clusters.append(cluster)
    return {"clusters": clusters, "resolution": resolution}


def plan_routes(
    records: Sequence[IncidentRecord],
    *,
    depot_lat: float,
    depot_lng: float,
    max_stops: int = 25,
) -> Dict[str, object]:
    """Generate a deterministic visiting order optimised for field crews."""

    stops: List[Dict[str, object]] = []
    for record in records:
        if record.latitud is None or record.longitud is None:
            continue

        distance = _haversine_km(depot_lat, depot_lng, record.latitud, record.longitud)
        bearing = math.atan2(record.longitud - depot_lng, record.latitud - depot_lat)
        stops.append(
            {
                "id": pseudoanonymize(record.anon_id or record.id, salt="route"),
                "lat": round(record.latitud, 6),
                "lng": round(record.longitud, 6),
                "categoria": record.categoria or "Sin categoría",
                "distrito": record.distrito or "Sin distrito",
                "distance_km": round(distance, 3),
                "bearing": bearing,
                "estado": _normalize_state(record.estado),
            }
        )

    if not stops:
        return {
            "depot": {"lat": depot_lat, "lng": depot_lng},
            "stops": [],
            "total_distance_km": 0.0,
        }

    ordered = sorted(stops, key=lambda stop: stop["bearing"])
    trimmed = ordered[:max_stops]

    total_distance = 0.0
    last_point = {"lat": depot_lat, "lng": depot_lng}
    for stop in trimmed:
        total_distance += _haversine_km(last_point["lat"], last_point["lng"], stop["lat"], stop["lng"])
        last_point = stop
    total_distance += _haversine_km(last_point["lat"], last_point["lng"], depot_lat, depot_lng)

    for index, stop in enumerate(trimmed, start=1):
        stop["orden"] = index

    return {
        "depot": {"lat": depot_lat, "lng": depot_lng},
        "stops": trimmed,
        "total_distance_km": round(total_distance, 3),
    }


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the distance in kilometres between two points."""

    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


__all__ = [
    "IncidentRecord",
    "load_incidents_for_municipio",
    "build_scorecards",
    "build_heatmap",
    "demand_forecast",
    "cluster_incidents",
    "plan_routes",
]
