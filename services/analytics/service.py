"""Business logic for analytics endpoints."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import func, literal

from extensions import db
from models import MunicipioTicket, PymePedido, PymeTicket, TicketComentario

from .cache import analytics_cache
from .config import get_config
from .filters import AnalyticsFilters
from .helpers import (
    compute_percentiles,
    compute_ctr,
    mode,
    safe_ratio,
    tenant_as_int,
    to_minutes,
)
from .repository import (
    fetch_cohorts,
    fetch_geo_cells,
    fetch_top_metrics,
    fetch_whatsapp_templates,
    load_municipio_dataset,
    load_pyme_dataset,
    load_users,
    municipio_ticket_query,
    pyme_pedido_query,
    pyme_ticket_query,
)


def _filters_cache_key(filters: AnalyticsFilters, *extra: Any) -> Tuple:
    return (
        filters.tenant_id,
        filters.scope,
        filters.date_from.isoformat() if filters.date_from else None,
        filters.date_to.isoformat() if filters.date_to else None,
        filters.canales,
        filters.categorias,
        filters.estados,
        filters.agentes,
        filters.zonas,
        filters.etiquetas,
        filters.rubros,
        filters.bbox,
        filters.pyme_ids,
        filters.resolution,
        extra,
    )


def _is_closed(status: Optional[str]) -> bool:
    if not status:
        return False
    return status.lower() in {"cerrado", "resuelto", "completado", "closed"}


def _is_automated(ticket) -> bool:
    origen = (getattr(ticket, "origen", None) or "").lower()
    canal = (getattr(ticket, "canal_ingreso", None) or "").lower()
    if origen in {"bot", "automático", "automatico"}:
        return True
    if canal in {"bot", "chatbot", "whatsapp_bot"}:
        return True
    return False


def _ticket_first_response_minutes(ticket, comments: Sequence[TicketComentario]) -> Optional[float]:
    if not comments:
        return None
    admin_comments = [c for c in comments if c.es_admin]
    if not admin_comments:
        return None
    first_comment = min(admin_comments, key=lambda c: c.fecha)
    delta = first_comment.fecha - ticket.fecha
    return to_minutes(delta)


def _ticket_resolution_minutes(ticket, comments: Sequence[TicketComentario]) -> Optional[float]:
    closed_comments = [
        c for c in comments if (c.estado_ticket or "").lower() in {"cerrado", "resuelto", "closed"}
    ]
    if closed_comments:
        close_comment = min(closed_comments, key=lambda c: c.fecha)
        return to_minutes(close_comment.fecha - ticket.fecha)
    if _is_closed(ticket.estado) and getattr(ticket, "ultima_actividad", None):
        return to_minutes(ticket.ultima_actividad - ticket.fecha)
    return None


def _ticket_reopened(comments: Sequence[TicketComentario]) -> bool:
    for comment in comments:
        if (comment.estado_ticket or "").lower() in {"reabierto", "reopened"}:
            return True
    return False


def _attachments_per_ticket(attachments_map: Dict[int, Sequence]) -> Tuple[int, int]:
    total = 0
    with_attachments = 0
    for ticket_id, attachments in attachments_map.items():
        count = len(attachments)
        total += count
        if count:
            with_attachments += 1
    return total, with_attachments


def _survey_metrics(surveys_map: Dict[int, Sequence]) -> Dict[str, Any]:
    scores = [survey.puntuacion for surveys in surveys_map.values() for survey in surveys]
    if not scores:
        return {"nps": None, "csat": None, "responses": 0}
    max_score = max(scores)
    result: Dict[str, Any] = {"responses": len(scores)}
    if max_score > 5:
        promoters = sum(1 for score in scores if score >= 9)
        detractors = sum(1 for score in scores if score <= 6)
        result["nps"] = round((promoters - detractors) / len(scores) * 100, 2)
    else:
        result["nps"] = None
    result["csat"] = round(sum(scores) / len(scores), 2)
    return result


def _default_summary_payload() -> Dict[str, Any]:
    return {
        "totals": {
            "tickets": 0,
            "tickets_abiertos": 0,
            "backlog": 0,
            "adjuntos_por_100": 0.0,
            "automatizado_pct": 0.0,
            "primer_contacto_pct": 0.0,
            "reaperturas": 0,
            "nps": None,
            "csat": None,
        },
        "sla": {
            "tta": {"p50": None, "p90": None, "p95": None},
            "ttr": {"p50": None, "p90": None, "p95": None},
        },
        "extras": {},
    }


def _municipio_summary(filters: AnalyticsFilters) -> Dict[str, Any]:
    tickets, comments_map, attachments_map, surveys_map = load_municipio_dataset(filters)
    payload = _default_summary_payload()
    total = len(tickets)
    if not total:
        return payload

    tta_values: List[float] = []
    ttr_values: List[float] = []
    reopenings = 0
    automated = 0
    first_contact_resolved = 0
    open_tickets = 0

    attachment_total, attachments_with_ticket = _attachments_per_ticket(attachments_map)

    channel_scores: Dict[str, List[int]] = defaultdict(list)

    for ticket in tickets:
        comments = comments_map.get(ticket.id, [])
        first_minutes = _ticket_first_response_minutes(ticket, comments)
        if first_minutes is not None:
            tta_values.append(first_minutes)
        resolution_minutes = _ticket_resolution_minutes(ticket, comments)
        if resolution_minutes is not None:
            ttr_values.append(resolution_minutes)
        if _ticket_reopened(comments):
            reopenings += 1
        if _is_automated(ticket):
            automated += 1
        admin_comments = [c for c in comments if c.es_admin]
        if admin_comments:
            responses = [c for c in admin_comments if c.es_admin]
            if len(responses) == 1 and not _ticket_reopened(comments):
                first_contact_resolved += 1
        if not _is_closed(ticket.estado):
            open_tickets += 1
        for survey in surveys_map.get(ticket.id, []):
            canal = (ticket.canal_ingreso or "desconocido").lower()
            channel_scores[canal].append(survey.puntuacion)

    survey_summary = _survey_metrics(surveys_map)

    payload["totals"].update(
        {
            "tickets": total,
            "tickets_abiertos": open_tickets,
            "backlog": open_tickets,
            "adjuntos_por_100": round((attachment_total / total) * 100, 2) if total else 0.0,
            "automatizado_pct": safe_ratio(automated, total),
            "primer_contacto_pct": safe_ratio(first_contact_resolved, total),
            "reaperturas": reopenings,
            "nps": survey_summary.get("nps"),
            "csat": survey_summary.get("csat"),
            "encuestas": survey_summary.get("responses"),
        }
    )

    payload["sla"]["tta"] = compute_percentiles(tta_values)
    payload["sla"]["ttr"] = compute_percentiles(ttr_values)

    payload["extras"]["canales"] = {
        canal: {
            "promedio": round(sum(scores) / len(scores), 2) if scores else None,
            "respuestas": len(scores),
        }
        for canal, scores in channel_scores.items()
    }

    return payload


def _parse_pedido_items(detalles_raw: str) -> List[Dict[str, Any]]:
    if not detalles_raw:
        return []
    try:
        data = json.loads(detalles_raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _customer_key(pedido: PymePedido) -> Optional[str]:
    for field in (pedido.user_id, pedido.email_cliente, pedido.telefono_cliente, pedido.nombre_cliente):
        if field:
            return str(field)
    return None


def _pyme_summary(filters: AnalyticsFilters) -> Dict[str, Any]:
    tickets, pedidos, comments_map, attachments_map, surveys_map = load_pyme_dataset(filters)
    payload = _default_summary_payload()
    total_tickets = len(tickets)
    total_orders = len(pedidos)

    tta_values: List[float] = []
    ttr_values: List[float] = []
    automated = 0
    reopenings = 0
    first_contact_resolved = 0

    attachment_total, _ = _attachments_per_ticket(attachments_map)

    for ticket in tickets:
        comments = comments_map.get(ticket.id, [])
        first_minutes = _ticket_first_response_minutes(ticket, comments)
        if first_minutes is not None:
            tta_values.append(first_minutes)
        resolution_minutes = _ticket_resolution_minutes(ticket, comments)
        if resolution_minutes is not None:
            ttr_values.append(resolution_minutes)
        if _ticket_reopened(comments):
            reopenings += 1
        if _is_automated(ticket):
            automated += 1
        admin_comments = [c for c in comments if c.es_admin]
        if admin_comments:
            if len(admin_comments) == 1 and not _ticket_reopened(comments):
                first_contact_resolved += 1

    total_amount = sum(pedido.monto_total or 0 for pedido in pedidos)
    avg_ticket = round(total_amount / total_orders, 2) if total_orders else 0.0

    customers: Dict[str, List[datetime]] = defaultdict(list)
    hourly_orders: Counter = Counter()
    for pedido in pedidos:
        hourly_orders[pedido.fecha.hour if pedido.fecha else 0] += 1
        key = _customer_key(pedido)
        if key:
            customers[key].append(pedido.fecha)

    retention30 = retention60 = retention90 = 0
    cohort_sizes = 0
    for order_dates in customers.values():
        if len(order_dates) < 2:
            continue
        cohort_sizes += 1
        sorted_dates = sorted(dt for dt in order_dates if dt)
        first = sorted_dates[0]
        for dt in sorted_dates[1:]:
            delta = (dt - first).days
            if delta <= 30:
                retention30 += 1
                break
            if delta <= 60:
                retention60 += 1
                break
            if delta <= 90:
                retention90 += 1
                break

    survey_summary = _survey_metrics(surveys_map)

    payload["totals"].update(
        {
            "tickets": total_tickets,
            "tickets_abiertos": sum(1 for ticket in tickets if not _is_closed(ticket.estado)),
            "backlog": sum(1 for ticket in tickets if not _is_closed(ticket.estado)),
            "pedidos": total_orders,
            "ticket_medio": avg_ticket,
            "conversion_pct": safe_ratio(total_orders, total_tickets) if total_tickets else 0.0,
            "adjuntos_por_100": round((attachment_total / total_tickets) * 100, 2) if total_tickets else 0.0,
            "automatizado_pct": safe_ratio(automated, total_tickets) if total_tickets else 0.0,
            "primer_contacto_pct": safe_ratio(first_contact_resolved, total_tickets) if total_tickets else 0.0,
            "reaperturas": reopenings,
            "nps": survey_summary.get("nps"),
            "csat": survey_summary.get("csat"),
            "encuestas": survey_summary.get("responses"),
            "retencion_30": safe_ratio(retention30, cohort_sizes),
            "retencion_60": safe_ratio(retention60, cohort_sizes),
            "retencion_90": safe_ratio(retention90, cohort_sizes),
            "hora_pico": mode(hourly_orders.elements()),
        }
    )

    payload["sla"]["tta"] = compute_percentiles(tta_values)
    payload["sla"]["ttr"] = compute_percentiles(ttr_values)

    return payload


def _operations_summary(filters: AnalyticsFilters) -> Dict[str, Any]:
    municipio_filters = filters
    pyme_filters = AnalyticsFilters(
        tenant_id=filters.tenant_id,
        scope="pyme",
        date_from=filters.date_from,
        date_to=filters.date_to,
        canales=filters.canales,
        categorias=filters.categorias,
        estados=filters.estados,
        agentes=filters.agentes,
        zonas=filters.zonas,
        etiquetas=filters.etiquetas,
        rubros=filters.rubros,
        bbox=filters.bbox,
        pyme_ids=filters.pyme_ids,
        resolution=filters.resolution,
    )
    municipio_data = _municipio_summary(municipio_filters)
    pyme_data = _pyme_summary(pyme_filters)

    tickets_total = municipio_data["totals"].get("tickets", 0) + pyme_data["totals"].get("tickets", 0)
    abiertos = (
        municipio_data["totals"].get("tickets_abiertos", 0)
        + pyme_data["totals"].get("tickets_abiertos", 0)
    )

    muni_dataset = load_municipio_dataset(filters)
    pyme_dataset = load_pyme_dataset(pyme_filters)
    now = datetime.utcnow()
    aging_buckets = {"0-4h": 0, "4-24h": 0, "1-3d": 0, "3-7d": 0, ">7d": 0}
    queue_by_estado: Counter[str] = Counter()
    agent_stats: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"tickets": 0, "first_response": []})

    def _collect_operations(dataset, is_pyme: bool = False):
        if is_pyme:
            tickets, _pedidos, comments_map, _attachments, _surveys = dataset
        else:
            tickets, comments_map, _attachments, _surveys = dataset
        for ticket in tickets:
            estado = (ticket.estado or "sin_estado").lower()
            queue_by_estado[estado] += 1
            if not _is_closed(ticket.estado) and ticket.fecha:
                delta_hours = (now - ticket.fecha).total_seconds() / 3600 if ticket.fecha else 0
                if delta_hours <= 4:
                    aging_buckets["0-4h"] += 1
                elif delta_hours <= 24:
                    aging_buckets["4-24h"] += 1
                elif delta_hours <= 72:
                    aging_buckets["1-3d"] += 1
                elif delta_hours <= 168:
                    aging_buckets["3-7d"] += 1
                else:
                    aging_buckets[">7d"] += 1
            comments = comments_map.get(ticket.id, [])
            admin_comments = [c for c in comments if c.es_admin and c.user_id]
            if admin_comments:
                first = min(admin_comments, key=lambda c: c.fecha)
                delta = to_minutes(first.fecha - ticket.fecha)
                agent_data = agent_stats[first.user_id]
                agent_data["tickets"] += 1
                if delta is not None:
                    agent_data["first_response"].append(delta)

    _collect_operations(muni_dataset, is_pyme=False)
    _collect_operations(pyme_dataset, is_pyme=True)

    agent_ids = list(agent_stats.keys())
    user_map = load_users(agent_ids)
    agent_rows = []
    for agent_id, data in agent_stats.items():
        if agent_id is None:
            continue
        name = getattr(user_map.get(agent_id), "name", f"Agente {agent_id}")
        responses = data["first_response"]
        avg_response = round(sum(responses) / len(responses), 2) if responses else None
        agent_rows.append(
            {
                "agente_id": agent_id,
                "agente": name,
                "tickets": data["tickets"],
                "respuesta_promedio_min": avg_response,
            }
        )
    agent_rows.sort(key=lambda row: row["tickets"], reverse=True)

    return {
        "totals": {
            "tickets": tickets_total,
            "abiertos": abiertos,
            "violaciones_sla": municipio_data["totals"].get("reaperturas", 0),
            "primer_contacto_pct": municipio_data["totals"].get("primer_contacto_pct", 0.0),
            "automatizado_pct": municipio_data["totals"].get("automatizado_pct", 0.0),
        },
        "sla": municipio_data.get("sla", {}),
        "extras": {
            "pyme": pyme_data,
            "aging": aging_buckets,
            "queue": dict(queue_by_estado),
            "agents": agent_rows,
        },
    }


def get_summary(filters: AnalyticsFilters) -> Dict[str, Any]:
    config = get_config()
    analytics_cache.ttl_seconds = config.cache_ttl_seconds
    cache_key = ("summary",) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _summary_no_cache(filters),
    )


def _summary_no_cache(filters: AnalyticsFilters) -> Dict[str, Any]:
    if filters.scope == "municipio":
        return _municipio_summary(filters)
    if filters.scope == "pyme":
        return _pyme_summary(filters)
    return _operations_summary(filters)


def _aggregate_timeseries(query, date_column, group_column=None) -> List[Dict[str, Any]]:
    base = query.with_entities(func.date(date_column).label("date"))
    if group_column is not None:
        base = base.add_columns(group_column.label("group"))
    else:
        base = base.add_columns(literal("total").label("group"))
    base = base.add_columns(func.count().label("value"))
    rows = base.group_by("date", "group").order_by("date").all()
    results: List[Dict[str, Any]] = []
    for row in rows:
        date_value = row.date.isoformat() if hasattr(row.date, "isoformat") else str(row.date)
        results.append({
            "date": date_value,
            "group": row.group,
            "value": float(row.value or 0),
        })
    return results


def get_timeseries(filters: AnalyticsFilters, metric: str = "tickets", group: Optional[str] = None) -> Dict[str, Any]:
    cache_key = ("timeseries", metric, group) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _timeseries_no_cache(filters, metric, group),
    )


def _timeseries_no_cache(filters: AnalyticsFilters, metric: str, group: Optional[str]) -> Dict[str, Any]:
    if filters.scope == "municipio":
        query = municipio_ticket_query(filters)
        group_column = None
        if group == "categoria":
            group_column = MunicipioTicket.categoria
        elif group == "canal":
            group_column = MunicipioTicket.canal_ingreso
        elif group == "estado":
            group_column = MunicipioTicket.estado
        data = _aggregate_timeseries(query, MunicipioTicket.fecha, group_column)
    elif filters.scope == "pyme":
        query = pyme_ticket_query(filters)
        group_column = None
        if group == "categoria":
            group_column = PymeTicket.categoria
        elif group == "estado":
            group_column = PymeTicket.estado
        data = _aggregate_timeseries(query, PymeTicket.fecha, group_column)
    else:
        query = municipio_ticket_query(filters)
        data = _aggregate_timeseries(query, MunicipioTicket.fecha)
    return {"series": data}


def get_breakdown(filters: AnalyticsFilters, dimension: str = "categoria") -> Dict[str, Any]:
    cache_key = ("breakdown", dimension) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _breakdown_no_cache(filters, dimension),
    )


def _breakdown_no_cache(filters: AnalyticsFilters, dimension: str) -> Dict[str, Any]:
    if filters.scope == "municipio":
        query = municipio_ticket_query(filters)
        column = {
            "categoria": MunicipioTicket.categoria,
            "canal": MunicipioTicket.canal_ingreso,
            "estado": MunicipioTicket.estado,
            "zona": MunicipioTicket.distrito,
        }.get(dimension, MunicipioTicket.categoria)
    else:
        query = pyme_ticket_query(filters)
        column = {
            "categoria": PymeTicket.categoria,
            "canal": getattr(PymeTicket, "canal", PymeTicket.categoria),
            "estado": PymeTicket.estado,
        }.get(dimension, PymeTicket.categoria)

    rows = (
        query.with_entities(column.label("label"), func.count().label("value"))
        .group_by(column)
        .order_by(func.count().desc())
        .all()
    )
    return {
        "breakdown": [
            {
                "label": row.label or "sin_dato",
                "value": int(row.value or 0),
            }
            for row in rows
        ]
    }


def _generate_geo_from_tickets(tickets: Sequence, filters: AnalyticsFilters) -> List[Dict[str, Any]]:
    try:
        import h3
    except ImportError:  # pragma: no cover - safety fallback
        cells: Dict[str, Dict[str, Any]] = {}
        for ticket in tickets:
            if ticket.latitud is None or ticket.longitud is None:
                continue
            lat = round(ticket.latitud, 3)
            lon = round(ticket.longitud, 3)
            cell_id = f"fallback_{lat}_{lon}"
            cell = cells.setdefault(
                cell_id,
                {
                    "cell_id": cell_id,
                    "count": 0,
                    "categories": defaultdict(int),
                    "centroid_lat": lat,
                    "centroid_lon": lon,
                },
            )
            cell["count"] += 1
            categoria = getattr(ticket, "categoria", None) or "sin_dato"
            cell["categories"][categoria] += 1
        return [
            {
                "cell_id": cell_id,
                "count": data["count"],
                "centroid_lat": data["centroid_lat"],
                "centroid_lon": data["centroid_lon"],
                "categories": dict(data["categories"]),
            }
            for cell_id, data in cells.items()
        ]

    resolution = filters.resolution
    cells: Dict[str, Dict[str, Any]] = {}

    if hasattr(h3, "geo_to_h3"):
        geo_to_h3 = h3.geo_to_h3  # type: ignore[attr-defined]

        def to_cell(lat: float, lon: float, res: int):
            return geo_to_h3(lat, lon, res)

    elif hasattr(h3, "latlng_to_cell"):
        latlng_to_cell = h3.latlng_to_cell  # type: ignore[attr-defined]

        def _try_call(options, lat: float, lon: float, res: int):
            last_error: TypeError | None = None
            for candidate in options:
                try:
                    return candidate(lat, lon, res)
                except TypeError as exc:
                    last_error = exc
                    continue
            if last_error is not None:
                raise TypeError("Unsupported h3.latlng_to_cell signature") from last_error
            raise AttributeError("No callable options supplied for h3.latlng_to_cell")

        call_options = [
            lambda lat, lon, res: latlng_to_cell(lat, lon, res),
            lambda lat, lon, res: latlng_to_cell((lat, lon), res),
        ]

        if hasattr(h3, "LatLng"):
            LatLng = h3.LatLng  # type: ignore[attr-defined]

            call_options.append(lambda lat, lon, res: latlng_to_cell(LatLng(lat, lon), res))

        # Determine which signature works once so that we don't pay the price on every call.
        def _resolve_caller():
            for candidate in call_options:
                try:
                    candidate(0.0, 0.0, 1)
                except TypeError:
                    continue
                except Exception:
                    # If the function executed but raised a different exception
                    # (e.g. because resolution is invalid), assume the signature
                    # itself is correct and use it.
                    return candidate
                else:
                    return candidate
            # If none of the candidates worked, let _try_call raise a helpful error.
            def _nope(lat: float, lon: float, res: int):  # pragma: no cover - defensive guard
                return _try_call(call_options, lat, lon, res)

            return _nope

        chosen = _resolve_caller()

        def to_cell(lat: float, lon: float, res: int):
            try:
                return chosen(lat, lon, res)
            except TypeError:
                # Fallback to trying all candidates in case runtime args
                # trigger a different valid overload (e.g. numpy scalars).
                return _try_call(call_options, lat, lon, res)
    else:  # pragma: no cover - unexpected API change
        raise AttributeError("h3 library does not expose geo_to_h3 or latlng_to_cell")

    if hasattr(h3, "h3_to_geo"):
        h3_to_geo = h3.h3_to_geo  # type: ignore[attr-defined]

        def to_latlng(cell_id: str):
            return h3_to_geo(cell_id)

    elif hasattr(h3, "cell_to_latlng"):
        cell_to_latlng = h3.cell_to_latlng  # type: ignore[attr-defined]

        def to_latlng(cell_id: str):
            return cell_to_latlng(cell_id)
    else:  # pragma: no cover - unexpected API change
        raise AttributeError("h3 library does not expose h3_to_geo or cell_to_latlng")

    def normalize_latlng(raw_value: Any) -> Tuple[float, float]:
        """Return a (lat, lon) tuple regardless of H3 API return type."""

        if isinstance(raw_value, dict):
            lat = raw_value.get("lat") or raw_value.get("latitude")
            lon = raw_value.get("lng") or raw_value.get("lon") or raw_value.get("longitude")
        elif hasattr(raw_value, "lat") and hasattr(raw_value, "lng"):
            lat = getattr(raw_value, "lat")
            lon = getattr(raw_value, "lng")
        elif hasattr(raw_value, "latitude") and hasattr(raw_value, "longitude"):
            lat = getattr(raw_value, "latitude")
            lon = getattr(raw_value, "longitude")
        else:
            try:
                lat, lon = raw_value  # type: ignore[misc]
            except (TypeError, ValueError):
                raise TypeError("Unexpected lat/lon format returned by H3 API") from None

        if lat is None or lon is None:
            raise TypeError("H3 API did not return both latitude and longitude")

        return float(lat), float(lon)

    for ticket in tickets:
        if ticket.latitud is None or ticket.longitud is None:
            continue
        cell_id = str(to_cell(ticket.latitud, ticket.longitud, resolution))
        cell = cells.setdefault(
            cell_id,
            {"cell_id": cell_id, "count": 0, "categories": defaultdict(int)},
        )
        cell["count"] += 1
        categoria = getattr(ticket, "categoria", None) or "sin_dato"
        cell["categories"][categoria] += 1
    results = []
    for cell_id, data in cells.items():
        lat, lon = normalize_latlng(to_latlng(cell_id))
        results.append(
            {
                "cell_id": cell_id,
                "count": data["count"],
                "centroid_lat": lat,
                "centroid_lon": lon,
                "categories": dict(data["categories"]),
            }
        )
    return results


def get_geo_heatmap(filters: AnalyticsFilters) -> Dict[str, Any]:
    cache_key = ("geo_heatmap",) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _geo_heatmap_no_cache(filters),
    )


def _geo_heatmap_no_cache(filters: AnalyticsFilters) -> Dict[str, Any]:
    cached_cells = fetch_geo_cells(filters)
    if cached_cells:
        return {
            "cells": [
                {
                    "cell_id": cell.cell_id,
                    "count": cell.count,
                    "centroid_lat": (cell.centroid or {}).get("centroid_lat") if cell.centroid else None,
                    "centroid_lon": (cell.centroid or {}).get("centroid_lon") if cell.centroid else None,
                    "categories": cell.categories or {},
                }
                for cell in cached_cells
            ]
        }

    if filters.scope == "municipio":
        tickets = municipio_ticket_query(filters).all()
    else:
        tickets = pyme_pedido_query(filters).all() if filters.scope == "pyme" else []
    return {"cells": _generate_geo_from_tickets(tickets, filters)}


def get_geo_points(filters: AnalyticsFilters, limit: int = 500) -> Dict[str, Any]:
    cache_key = ("geo_points", limit) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _geo_points_no_cache(filters, limit),
    )


def _geo_points_no_cache(filters: AnalyticsFilters, limit: int) -> Dict[str, Any]:
    if filters.scope == "municipio":
        tickets = municipio_ticket_query(filters).limit(limit).all()
        points = [
            {
                "lat": ticket.latitud,
                "lon": ticket.longitud,
                "categoria": ticket.categoria,
                "estado": ticket.estado,
            }
            for ticket in tickets
            if ticket.latitud is not None and ticket.longitud is not None
        ]
    else:
        pedidos = pyme_pedido_query(filters).limit(limit).all()
        points = [
            {
                "lat": pedido.latitud,
                "lon": pedido.longitud,
                "total": pedido.monto_total,
                "estado": pedido.estado,
            }
            for pedido in pedidos
            if pedido.latitud is not None and pedido.longitud is not None
        ]
    return {"points": points}


def get_top(filters: AnalyticsFilters, category: str = "barrios", limit: int = 10) -> Dict[str, Any]:
    cache_key = ("top", category, limit) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _top_no_cache(filters, category, limit),
    )


def _top_no_cache(filters: AnalyticsFilters, category: str, limit: int) -> Dict[str, Any]:
    cached = fetch_top_metrics(filters, category)
    if cached:
        rows = [
            {
                "label": metric.label,
                "value": metric.value,
                "delta": metric.delta,
                "metadata": metric.details,
            }
            for metric in cached[:limit]
        ]
        return {"items": rows}

    if filters.scope == "municipio":
        query = municipio_ticket_query(filters)
        if category == "barrios":
            column = MunicipioTicket.distrito
        elif category == "calles":
            column = MunicipioTicket.direccion
        else:
            column = MunicipioTicket.categoria
    else:
        query = pyme_pedido_query(filters)
        if category == "productos":
            column = PymePedido.detalles
        else:
            column = PymePedido.estado

    if category == "productos":
        pedidos = query.all()
        counter: Counter = Counter()
        for pedido in pedidos:
            for item in _parse_pedido_items(pedido.detalles):
                nombre = item.get("nombre") or item.get("sku") or "sin_dato"
                counter[nombre] += item.get("qty", 1)
        rows = counter.most_common(limit)
        return {"items": [{"label": label, "value": value} for label, value in rows]}

    rows = (
        query.with_entities(column.label("label"), func.count().label("value"))
        .group_by(column)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "label": row.label or "sin_dato",
                "value": int(row.value or 0),
            }
            for row in rows
        ]
    }


def get_operations_overview(filters: AnalyticsFilters) -> Dict[str, Any]:
    cache_key = ("operations",) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _operations_summary(filters),
    )


def get_cohorts(filters: AnalyticsFilters) -> Dict[str, Any]:
    cache_key = ("cohorts",) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _cohorts_no_cache(filters),
    )


def _cohorts_no_cache(filters: AnalyticsFilters) -> Dict[str, Any]:
    cached = fetch_cohorts(filters)
    if cached:
        return {
            "cohorts": [
                {
                    "cohort": row.cohort_key,
                    "size": row.size,
                    "retention30": row.retention_30,
                    "retention60": row.retention_60,
                    "retention90": row.retention_90,
                    "metadata": row.extra or {},
                }
                for row in cached
            ]
        }

    pedidos = pyme_pedido_query(filters).all()
    cohorts: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"size": 0, "retention": {30: 0, 60: 0, 90: 0}})
    customers: Dict[str, List[datetime]] = defaultdict(list)
    for pedido in pedidos:
        cohort_key = pedido.fecha.strftime("%Y-%m") if pedido.fecha else "desconocido"
        cohorts[cohort_key]["size"] += 1
        key = _customer_key(pedido)
        if key and pedido.fecha:
            customers[key].append(pedido.fecha)

    for order_dates in customers.values():
        if len(order_dates) < 2:
            continue
        sorted_dates = sorted(order_dates)
        first = sorted_dates[0]
        for dt in sorted_dates[1:]:
            delta = (dt - first).days
            if delta <= 30:
                cohorts[first.strftime("%Y-%m")]["retention"][30] += 1
            if delta <= 60:
                cohorts[first.strftime("%Y-%m")]["retention"][60] += 1
            if delta <= 90:
                cohorts[first.strftime("%Y-%m")]["retention"][90] += 1
            break

    response = {
        "cohorts": [
            {
                "cohort": cohort,
                "size": values["size"],
                "retention30": safe_ratio(values["retention"][30], values["size"]),
                "retention60": safe_ratio(values["retention"][60], values["size"]),
                "retention90": safe_ratio(values["retention"][90], values["size"]),
            }
            for cohort, values in sorted(cohorts.items())
        ]
    }
    return response


def get_whatsapp_templates(filters: AnalyticsFilters) -> Dict[str, Any]:
    cache_key = ("whatsapp_templates",) + _filters_cache_key(filters)
    return analytics_cache.get_or_set(
        cache_key,
        lambda: _whatsapp_templates_no_cache(filters),
    )


def _whatsapp_templates_no_cache(filters: AnalyticsFilters) -> Dict[str, Any]:
    cached = fetch_whatsapp_templates(filters)
    rows = []
    if cached:
        for row in cached:
            ctr = compute_ctr(row.sent, row.responded)
            rows.append(
                {
                    "template": row.template_name,
                    "date": row.metric_date.isoformat(),
                    "sent": row.sent,
                    "delivered": row.delivered,
                    "read": row.read,
                    "responded": row.responded,
                    "blocked": row.blocked,
                    "ctr": ctr,
                    "metadata": row.extra or {},
                }
            )
        return {"templates": rows}

    # Fallback to dynamic query based on pedidos interactions (approximation)
    pedidos = pyme_pedido_query(filters).all()
    counter: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for pedido in pedidos:
        canal = getattr(pedido, "canal", None) or "desconocido"
        counter[canal]["sent"] += 1
        if getattr(pedido, "estado", "").lower() in {"completado", "cerrado"}:
            counter[canal]["delivered"] += 1
            counter[canal]["responded"] += 1
    rows = [
        {
            "template": canal,
            "date": filters.date_from.date().isoformat() if filters.date_from else "",
            "sent": counts["sent"],
            "delivered": counts["delivered"],
            "read": counts.get("read", counts["delivered"]),
            "responded": counts.get("responded", 0),
            "blocked": counts.get("blocked", 0),
            "ctr": compute_ctr(counts["sent"], counts.get("responded", 0)),
        }
        for canal, counts in counter.items()
    ]
    return {"templates": rows}
