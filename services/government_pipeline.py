"""Reusable analytics pipeline for municipal innovation projects."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
from flask import current_app, has_app_context
import h3
from extensions import db
from models import MunicipioTicket, TenantProfile
from services.employee_ticket_access import apply_employee_ticket_category_scope
from utils.lazy_module import LazyModule

pd = LazyModule("pandas")
from services.tenant_ticket_scope import (
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
    tenant_owner_ids,
)
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
    tenant_id: Optional[int] = None,
    actor=None,
) -> List[IncidentRecord]:
    """Fetch incidents for the given municipality and convert them to records."""

    tenant = None
    try:
        normalized_owner_id = int(municipio_id)
    except (TypeError, ValueError):
        return []
    if tenant_id is not None:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            tenant = None
        if tenant is None or normalized_owner_id not in tenant_owner_ids(tenant):
            return []
    else:
        try:
            resolution = resolve_unique_tenant_for_owner(normalized_owner_id)
        except ValueError:
            resolution = None
        if resolution and resolution.status == "unique" and resolution.tenant is not None:
            tenant = resolution.tenant
        else:
            tenant = (
                db.session.get(TenantProfile, normalized_owner_id)
                or TenantProfile.query.filter_by(municipio_id=normalized_owner_id).first()
                or (
                    actor
                    and getattr(actor, "tenant_slug", None)
                    and TenantProfile.query.filter_by(slug=str(actor.tenant_slug).strip().lower()).first()
                )
            )
        if tenant is None:
            return []

    query = scoped_municipio_ticket_query(
        tenant,
        query=db.session.query(MunicipioTicket),
    )
    query = apply_employee_ticket_category_scope(query, actor, MunicipioTicket)
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




SECRETARIAS_MAP = {
    "Alumbrado y Electromecánica": ["luminaria", "farola", "alumbrado", "luz", "cable", "transformador", "electric"],
    "Obras Públicas y Bacheo": ["bache", "calle", "asfalto", "pavimento", "vereda", "cloaca", "agua", "obra", "cordon"],
    "Espacios Verdes y Arbolado": ["poda", "arbol", "árbol", "plaza", "cesped", "césped", "desmalez", "parque", "rama"],
    "Higiene Urbana y Limpieza": ["basura", "contenedor", "residuo", "microbasural", "barrido", "escombro", "limpieza"],
    "Tránsito y Seguridad Vial": ["semaforo", "semáforo", "señal", "estacionamiento", "loma", "transito", "tránsito", "vial"],
    "Salud, Zoonosis y Ambiente": ["perro", "zoonosis", "plaga", "fumiga", "ruido", "salud", "animal", "vacuna"],
}


def _classify_secretaria(categoria: Optional[str]) -> str:
    if not categoria:
        return "Atención Ciudadana General"
    cat_lower = categoria.lower()
    for sec_name, keywords in SECRETARIAS_MAP.items():
        if any(kw in cat_lower for kw in keywords):
            return sec_name
    return "Atención Ciudadana General"


def build_secretarias_traffic_light(records: Sequence[IncidentRecord]) -> Dict[str, Any]:
    """Calculate SLA performance and operational traffic light for each municipal area."""
    by_sec: Dict[str, List[IncidentRecord]] = {
        "Alumbrado y Electromecánica": [],
        "Obras Públicas y Bacheo": [],
        "Espacios Verdes y Arbolado": [],
        "Higiene Urbana y Limpieza": [],
        "Tránsito y Seguridad Vial": [],
        "Salud, Zoonosis y Ambiente": [],
        "Atención Ciudadana General": [],
    }

    for r in records:
        sec = _classify_secretaria(r.categoria)
        by_sec.setdefault(sec, []).append(r)

    secretarias_data = []
    total_incidents = len(records)
    total_resolved = 0
    total_sla_on_time = 0

    for sec_name, sec_records in by_sec.items():
        total = len(sec_records)
        if total == 0:
            continue

        resolved_records = [r for r in sec_records if _is_closed(r.estado)]
        resolved_count = len(resolved_records)
        pending_count = total - resolved_count
        total_resolved += resolved_count

        res_hours = [_ticket_resolution_hours(r) for r in resolved_records if _ticket_resolution_hours(r) is not None]
        avg_hours = round(mean(res_hours), 1) if res_hours else None

        on_time = [h for h in res_hours if h <= 48.0]
        on_time_count = len(on_time)
        total_sla_on_time += on_time_count

        sla_rate = round((on_time_count / resolved_count) * 100, 1) if resolved_count > 0 else 0.0
        resolution_rate = round((resolved_count / total) * 100, 1) if total > 0 else 0.0

        if resolution_rate >= 75.0 and (sla_rate >= 70.0 or avg_hours is None or avg_hours <= 48.0):
            status_color = "green"
            status_label = "Óptimo"
        elif resolution_rate >= 45.0:
            status_color = "yellow"
            status_label = "En Observación"
        else:
            status_color = "red"
            status_label = "Crítico / Demorado"

        csat_score = min(5.0, max(3.0, round(3.5 + (resolution_rate / 100.0) * 1.5, 1)))

        secretarias_data.append({
            "secretaria": sec_name,
            "total_reclamos": total,
            "resueltos": resolved_count,
            "pendientes": pending_count,
            "porcentaje_resolucion": resolution_rate,
            "tiempo_promedio_horas": avg_hours or 36.0,
            "cumplimiento_sla_porcentaje": sla_rate,
            "semaforo": status_color,
            "estado_rendimiento": status_label,
            "csat_estimado": csat_score,
        })

    secretarias_data.sort(key=lambda s: s["total_reclamos"], reverse=True)

    global_res_rate = round((total_resolved / total_incidents) * 100, 1) if total_incidents > 0 else 0.0
    global_sla_rate = round((total_sla_on_time / total_resolved) * 100, 1) if total_resolved > 0 else 0.0

    global_status = "green" if global_res_rate >= 70.0 else ("yellow" if global_res_rate >= 40.0 else "red")

    return {
        "resumen_general": {
            "total_reclamos": total_incidents,
            "total_resueltos": total_resolved,
            "tasa_resolucion_global": global_res_rate,
            "cumplimiento_sla_global": global_sla_rate,
            "semaforo_gobierno": global_status,
            "secretarias_evaluadas": len(secretarias_data),
        },
        "ranking_secretarias": secretarias_data,
    }


def detect_crisis_sentinel_anomalies(
    records: Sequence[IncidentRecord],
    *,
    window_days: int = 30,
) -> Dict[str, Any]:
    """Algorithmic early-detection sentinel for civic crises and spatiotemporal clusters."""
    if not records:
        return {
            "estado_centinela": "NORMAL",
            "nivel_amenaza": "BAJO",
            "alertas_activas": [],
            "total_alertas": 0,
        }

    alerts = []
    
    # 1. District density anomalies
    distrito_counts = Counter(r.distrito for r in records if r.distrito)
    for dist, count in distrito_counts.most_common(5):
        if count >= 8:
            alerts.append({
                "alerta_id": f"ALERT-DIST-{abs(hash(dist)) % 10000:04d}",
                "tipo": "ALTA_CONCENTRACION_TERRITORIAL",
                "severidad": "ALTA" if count >= 15 else "MEDIA",
                "distrito": dist,
                "categoria_principal": "Múltiples servicios",
                "reclamos_afectados": count,
                "resumen": f"Concentración inusual de {count} reclamos en el distrito {dist}.",
                "accion_recomendada": f"Despachar cuadrilla móvil de inspección territorial a {dist}.",
            })

    # 2. Category volume anomalies
    cat_counts = Counter(r.categoria for r in records if r.categoria)
    for cat, count in cat_counts.most_common(5):
        if count >= 10:
            alerts.append({
                "alerta_id": f"ALERT-CAT-{abs(hash(cat)) % 10000:04d}",
                "tipo": "PICO_DEMANDA_CATEGORIA",
                "severidad": "CRITICA" if count >= 20 else "ALTA",
                "distrito": "Interdistrital / Todo el Municipio",
                "categoria_principal": cat,
                "reclamos_afectados": count,
                "resumen": f"Pico de demanda con {count} reclamos en la categoría '{cat}'.",
                "accion_recomendada": f"Reforzar turnos y stock de insumos para el área de {cat}.",
            })

    sentinel_status = "NORMAL"
    threat_level = "BAJO"
    if any(a["severidad"] == "CRITICA" for a in alerts):
        sentinel_status = "CRISIS_DETECTADA"
        threat_level = "CRITICO"
    elif any(a["severidad"] == "ALTA" for a in alerts):
        sentinel_status = "ALERTA_PREVENTIVA"
        threat_level = "MEDIO_ALTO"

    return {
        "estado_centinela": sentinel_status,
        "nivel_amenaza": threat_level,
        "alertas_activas": alerts,
        "total_alertas": len(alerts),
        "escaneado_en": get_local_now().isoformat(),
    }


__all__ = [
    "IncidentRecord",
    "load_incidents_for_municipio",
    "build_scorecards",
    "build_heatmap",
    "demand_forecast",
    "cluster_incidents",
    "plan_routes",
    "build_secretarias_traffic_light",
    "detect_crisis_sentinel_anomalies",
]
