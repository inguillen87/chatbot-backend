"""Admin analytics endpoints with tenant-scoped access and exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy import func, or_

from models import AnalyticsEventV2, EncComentario, EncEncuesta, EncRespuesta, MunicipioTicket, PymeTicket, TicketComentario
from services.analytics import get_geo_heatmap, get_summary
from services.analytics.filters import parse_filters
from services.analytics.rbac import require_access

admin_analytics_bp = Blueprint("admin_analytics", __name__, url_prefix="/admin/analytics")

_DASHBOARD_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_CACHE_TTL_SECONDS = 20.0
_ANALYTICS_HUB_CONTRACT_VERSION = "2026-analytics-hub-v2"
_HEATMAP_CATEGORY_COLORS = [
    "#EF4444",
    "#F97316",
    "#EAB308",
    "#22C55E",
    "#06B6D4",
    "#3B82F6",
    "#8B5CF6",
    "#EC4899",
]


def _json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    return response


def _tenant_id_as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be numeric")


def _ensure_total_interactions(payload: dict[str, Any]) -> dict[str, Any]:
    totals = payload.setdefault("totals", {})
    if "total_interactions" not in totals:
        tickets = int(totals.get("tickets") or 0)
        pedidos = int(totals.get("pedidos") or 0)
        encuestas = int(totals.get("encuestas") or 0)
        totals["total_interactions"] = tickets + pedidos + encuestas
    return payload


def _build_dashboard_payload(filters) -> dict[str, Any]:
    overview = _ensure_total_interactions(get_summary(filters))
    totals = overview.get("totals") or {}
    geo = get_geo_heatmap(filters)

    return {
        "tenant_id": filters.tenant_id,
        "scope": filters.scope,
        "period": {
            "from": filters.date_from.isoformat() if filters.date_from else None,
            "to": filters.date_to.isoformat() if filters.date_to else None,
        },
        "sections": {
            "general": overview,
            "municipio": overview if filters.scope == "municipio" else {"totals": totals},
            "ventas": overview if filters.scope == "pyme" else {"totals": totals},
            "mapas": {"geo": geo},
        },
        "navigation": {
            "primary": [
                {"key": "analytics", "label": "Analytics & Insights", "path": "/analytics", "active": True},
                {"key": "estadisticas", "label": "Estadísticas", "path": "/estadisticas", "active": False},
                {"key": "encuestas", "label": "Encuestas", "path": "/admin/encuestas", "active": False},
            ],
            "encuestas": {
                "admin_list_endpoint": "/api/admin/encuestas",
                "templates_endpoint": "/api/admin/encuestas/templates",
                "seed_demo_endpoint_template": "/api/admin/encuestas/{encuesta_id}/seed-demo",
                "public_results_endpoint_template": "/api/public/encuestas/{slug}/live-results",
            },
        },
    }


def _dashboard_cache_key(filters) -> str:
    return "|".join(
        [
            str(filters.tenant_id),
            str(filters.scope),
            str(filters.date_from.isoformat() if filters.date_from else ""),
            str(filters.date_to.isoformat() if filters.date_to else ""),
            ",".join(filters.canales),
            ",".join(filters.categorias),
            ",".join(filters.estados),
            str(filters.bbox or ""),
            str(filters.resolution),
        ]
    )


def _etag_for_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _request_id() -> str:
    inbound = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return inbound or uuid.uuid4().hex




def _bucket_age(age: int | None) -> str:
    if age is None:
        return "sin_dato"
    if age < 18:
        return "0-17"
    if age < 25:
        return "18-24"
    if age < 35:
        return "25-34"
    if age < 45:
        return "35-44"
    if age < 60:
        return "45-59"
    return "60+"


def _aggregate_heatmap_segments(events: list[dict[str, Any]]) -> dict[str, list[dict[str, int | str]]]:
    counters = {
        "categoria": {},
        "barrio": {},
        "distrito": {},
        "sexo": {},
        "rango_edad": {},
        "canal": {},
    }

    def inc(group: str, key: str):
        key = (key or "sin_dato").strip() or "sin_dato"
        counters[group][key] = counters[group].get(key, 0) + 1

    for event in events:
        labels = _event_segment_labels(event)
        inc("categoria", labels.get("categoria", "sin_dato"))
        inc("barrio", labels.get("barrio", "sin_dato"))
        inc("distrito", labels.get("distrito", "sin_dato"))
        inc("sexo", labels.get("sexo", "sin_dato"))
        inc("rango_edad", labels.get("rango_edad", "sin_dato"))
        inc("canal", labels.get("canal", "sin_dato"))

    return {
        group: [
            {"label": label, "count": count}
            for label, count in sorted(values.items(), key=lambda item: item[1], reverse=True)
        ]
        for group, values in counters.items()
    }




def _extract_segment_filters() -> dict[str, set[str]]:
    mapping = {
        "categoria": {item.strip().lower() for item in (request.args.get("categoria") or request.args.get("categorias") or "").split(",") if item.strip()},
        "barrio": {item.strip().lower() for item in (request.args.get("barrio") or "").split(",") if item.strip()},
        "distrito": {item.strip().lower() for item in (request.args.get("distrito") or "").split(",") if item.strip()},
        "sexo": {item.strip().lower() for item in (request.args.get("sexo") or request.args.get("genero") or "").split(",") if item.strip()},
        "rango_edad": {item.strip().lower() for item in (request.args.get("rango_edad") or "").split(",") if item.strip()},
        "canal": {item.strip().lower() for item in (request.args.get("canal") or "").split(",") if item.strip()},
    }
    return {k: v for k, v in mapping.items() if v}


def _event_segment_labels(event: dict[str, Any]) -> dict[str, str]:
    md = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    categoria = str(md.get("categoria") or md.get("category") or "sin_dato").strip().lower()
    barrio = str(md.get("barrio") or md.get("neighborhood") or "sin_dato").strip().lower()
    distrito = str(md.get("distrito") or md.get("district") or "sin_dato").strip().lower()
    sexo = str(md.get("sexo") or md.get("genero") or md.get("gender") or "sin_dato").strip().lower()

    edad_value = md.get("edad")
    try:
        edad_int = int(edad_value) if edad_value is not None else None
    except (TypeError, ValueError):
        edad_int = None

    canal = str(event.get("channel") or md.get("channel") or "sin_dato").strip().lower()
    return {
        "categoria": categoria,
        "barrio": barrio,
        "distrito": distrito,
        "sexo": sexo,
        "rango_edad": _bucket_age(edad_int).lower(),
        "canal": canal,
    }


def _event_matches_segment_filters(event: dict[str, Any], filters_map: dict[str, set[str]]) -> bool:
    if not filters_map:
        return True
    labels = _event_segment_labels(event)
    for key, allowed in filters_map.items():
        if allowed and labels.get(key, "sin_dato") not in allowed:
            return False
    return True


def _build_period_comparison(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"enabled": False, "reason": "no_events"}

    timestamps = [event.get("ts") for event in events if isinstance(event.get("ts"), datetime)]
    if len(timestamps) < 2:
        return {"enabled": False, "reason": "insufficient_timespan"}

    min_ts = min(timestamps)
    max_ts = max(timestamps)
    midpoint = min_ts + (max_ts - min_ts) / 2

    current = [event for event in events if isinstance(event.get("ts"), datetime) and event["ts"] >= midpoint]
    previous = [event for event in events if isinstance(event.get("ts"), datetime) and event["ts"] < midpoint]

    current_segments = _aggregate_heatmap_segments(current)
    previous_segments = _aggregate_heatmap_segments(previous)

    return {
        "enabled": True,
        "window": {"from": min_ts.isoformat(), "to": max_ts.isoformat(), "split_at": midpoint.isoformat()},
        "totals": {"current": len(current), "previous": len(previous), "delta": len(current) - len(previous)},
        "segments": {"current": current_segments, "previous": previous_segments},
    }


def _build_hotspots(events: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}

    for event in events:
        labels = _event_segment_labels(event)
        key = (labels.get("categoria", "sin_dato"), labels.get("barrio", "sin_dato"), labels.get("distrito", "sin_dato"))
        item = grouped.setdefault(
            key,
            {
                "categoria": key[0],
                "barrio": key[1],
                "distrito": key[2],
                "count": 0,
                "channels": {},
            },
        )
        item["count"] += 1
        channel = labels.get("canal", "sin_dato")
        channels = item["channels"]
        channels[channel] = channels.get(channel, 0) + 1

    ranked = sorted(grouped.values(), key=lambda value: value["count"], reverse=True)[:limit]
    max_count = ranked[0]["count"] if ranked else 1
    for item in ranked:
        item["hotspot_score"] = round((item["count"] / max_count) * 100, 2)
    return ranked


def _extract_vote_weight(metadata: dict[str, Any]) -> float:
    candidates = (
        metadata.get("votos"),
        metadata.get("cantidad_votos"),
        metadata.get("vote_count"),
        metadata.get("peso"),
        metadata.get("weight"),
        metadata.get("score"),
        metadata.get("puntaje"),
        metadata.get("valor"),
        metadata.get("total"),
    )
    for candidate in candidates:
        try:
            parsed = float(candidate)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 1.0


def _build_leaflet_heatmap_layers(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}

    for event in events:
        md = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        lat = md.get("lat") if md.get("lat") is not None else md.get("latitude")
        lng = md.get("lng") if md.get("lng") is not None else md.get("lon")
        if lng is None:
            lng = md.get("longitude")
        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            continue

        labels = _event_segment_labels(event)
        categoria = labels.get("categoria", "sin_dato")
        weight = _extract_vote_weight(md)

        bucket = by_category.setdefault(categoria, {"count": 0, "weight": 0.0, "points": []})
        bucket["count"] += 1
        bucket["weight"] += weight
        bucket["points"].append({"lat": lat, "lng": lng, "weight": round(weight, 4)})

    ranked = sorted(by_category.items(), key=lambda item: item[1]["weight"], reverse=True)
    if not ranked:
        return {
            "provider": "leaflet",
            "tiles": {
                "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap contributors",
            },
            "categories": [],
            "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": 0},
        }

    max_weight = max(item[1]["weight"] for item in ranked) or 1.0
    categories = []
    for index, (categoria, payload) in enumerate(ranked):
        color = _HEATMAP_CATEGORY_COLORS[index % len(_HEATMAP_CATEGORY_COLORS)]
        normalized = round(float(payload["weight"]) / max_weight, 4)
        categories.append(
            {
                "categoria": categoria,
                "color": color,
                "event_count": int(payload["count"]),
                "total_weight": round(float(payload["weight"]), 4),
                "intensity": normalized,
                "points": payload["points"],
            }
        )

    return {
        "provider": "leaflet",
        "tiles": {
            "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "© OpenStreetMap contributors",
        },
        "categories": categories,
        "legend": {
            "mode": "category_weight",
            "min_weight": 0,
            "max_weight": round(max_weight, 4),
        },
    }


def _dashboard_response(filters):
    cache_key = _dashboard_cache_key(filters)
    now = time.time()
    req_id = _request_id()
    entry = _DASHBOARD_CACHE.get(cache_key)
    cache_hit = bool(entry and float(entry.get("expires_at") or 0.0) > now)

    if cache_hit:
        payload = entry["payload"]
        expires_at = float(entry.get("expires_at") or now)
    else:
        payload = _build_dashboard_payload(filters)
        expires_at = now + _DASHBOARD_CACHE_TTL_SECONDS
        _DASHBOARD_CACHE[cache_key] = {
            "payload": payload,
            "expires_at": expires_at,
        }

    generated_at = entry.get("generated_at") if entry else None
    if not generated_at:
        generated_at = datetime.now(timezone.utc).isoformat()
        if cache_key in _DASHBOARD_CACHE:
            _DASHBOARD_CACHE[cache_key]["generated_at"] = generated_at

    enriched_payload = dict(payload)
    enriched_payload["meta"] = {
        "contract_version": _ANALYTICS_HUB_CONTRACT_VERSION,
        "generated_at": generated_at,
        "request_id": req_id,
        "cache": {
            "hit": cache_hit,
            "ttl_seconds": max(0, int(round(expires_at - now))),
        },
    }

    etag_source = dict(enriched_payload)
    etag_source["meta"] = {
        "contract_version": _ANALYTICS_HUB_CONTRACT_VERSION,
        "generated_at": generated_at,
    }
    etag = _etag_for_payload(etag_source)
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=20",
        "X-Analytics-Request-Id": req_id,
        "X-Analytics-Contract-Version": _ANALYTICS_HUB_CONTRACT_VERSION,
    }

    if request.if_none_match and request.if_none_match.contains(etag):
        return Response(status=304, headers=headers)

    response = _json(enriched_payload)
    for key, value in headers.items():
        response.headers[key] = value
    return response


@admin_analytics_bp.get("/overview")
def admin_analytics_overview():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    payload = _ensure_total_interactions(get_summary(filters))
    return _json(payload)


@admin_analytics_bp.get("/heatmap")
def admin_analytics_heatmap():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    tz = request.args.get("tz") or "UTC"
    base = get_geo_heatmap(filters)

    tenant_id = _tenant_id_as_int(filters.tenant_id)
    query = AnalyticsEventV2.query.filter(AnalyticsEventV2.tenant_id == tenant_id)
    if filters.date_from:
        query = query.filter(AnalyticsEventV2.ts >= filters.date_from)
    if filters.date_to:
        query = query.filter(AnalyticsEventV2.ts <= filters.date_to)

    temporal_rows = (
        query.with_entities(
            func.strftime("%w", AnalyticsEventV2.ts).label("weekday"),
            func.strftime("%H", AnalyticsEventV2.ts).label("hour"),
            func.count(AnalyticsEventV2.id).label("total"),
        )
        .group_by("weekday", "hour")
        .all()
    )

    events = query.with_entities(
        AnalyticsEventV2.channel.label("channel"),
        AnalyticsEventV2.metadata_payload.label("metadata"),
        AnalyticsEventV2.ts.label("ts"),
    ).all()


    temporal = [
        {"weekday": int(row.weekday), "hour": int(row.hour), "count": int(row.total)}
        for row in temporal_rows
        if row.weekday is not None and row.hour is not None
    ]

    segment_events = [{"channel": row.channel, "metadata": row.metadata, "ts": row.ts} for row in events]
    segment_filters = _extract_segment_filters()
    filtered_events = [event for event in segment_events if _event_matches_segment_filters(event, segment_filters)]

    return _json({
        "geo": base,
        "geo_layers": _build_leaflet_heatmap_layers(filtered_events),
        "temporal": temporal,
        "segments": _aggregate_heatmap_segments(filtered_events),
        "segments_filters_applied": {k: sorted(v) for k, v in segment_filters.items()},
        "period_comparison": _build_period_comparison(filtered_events),
        "hotspots": _build_hotspots(filtered_events),
        "tz": tz,
    })





def _coerce_window_minutes(value: Any, *, default: int = 30) -> int:
    """Parse window_minutes defensively to avoid 500s on malformed query params."""

    try:
        parsed = int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        parsed = default
    return max(5, min(parsed, 24 * 60))

def _build_realtime_hub_payload(filters, *, window_minutes: int = 30) -> dict[str, Any]:
    tenant_id = _tenant_id_as_int(filters.tenant_id)
    window_minutes = _coerce_window_minutes(window_minutes)
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)

    query = AnalyticsEventV2.query.filter(AnalyticsEventV2.tenant_id == tenant_id).filter(AnalyticsEventV2.ts >= cutoff)
    events = query.with_entities(
        AnalyticsEventV2.event_name.label("event_name"),
        AnalyticsEventV2.channel.label("channel"),
        AnalyticsEventV2.metadata_payload.label("metadata"),
        AnalyticsEventV2.ts.label("ts"),
        AnalyticsEventV2.lat.label("lat"),
        AnalyticsEventV2.lng.label("lng"),
    ).all()

    total_events = len(events)
    channel_counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    sentiment = {"positive": 0, "neutral": 0, "negative": 0}
    points = []

    for row in events:
        ch = (row.channel or "unknown").lower()
        channel_counts[ch] = channel_counts.get(ch, 0) + 1
        ev = (row.event_name or "unknown").lower()
        event_counts[ev] = event_counts.get(ev, 0) + 1
        md = row.metadata if isinstance(row.metadata, dict) else {}
        sent = str(md.get("sentiment") or md.get("sentimiento") or "").lower()
        if sent in sentiment:
            sentiment[sent] += 1
        if row.lat is not None and row.lng is not None:
            points.append({"lat": float(row.lat), "lng": float(row.lng), "weight": 1})

    survey_responses = EncRespuesta.query.filter_by(tenant_id=tenant_id).filter(EncRespuesta.created_at >= cutoff).count()
    survey_comments = (
        EncComentario.query.join(EncEncuesta, EncComentario.encuesta_id == EncEncuesta.id)
        .filter(EncEncuesta.tenant_id == tenant_id)
        .filter(EncComentario.created_at >= cutoff)
        .count()
    )

    live_chat_comments = (
        TicketComentario.query
        .outerjoin(MunicipioTicket, TicketComentario.municipio_ticket_id == MunicipioTicket.id)
        .outerjoin(PymeTicket, TicketComentario.pyme_ticket_id == PymeTicket.id)
        .filter(TicketComentario.fecha >= cutoff)
        .filter(
            or_(
                MunicipioTicket.tenant_id == tenant_id,
                MunicipioTicket.municipio_id == tenant_id,
                PymeTicket.tenant_id == tenant_id,
            )
        )
        .count()
    )

    top_events = [
        {"event": name, "count": count}
        for name, count in sorted(event_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    ]
    top_channels = [
        {"channel": name, "count": count}
        for name, count in sorted(channel_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    ]

    return {
        "tenant_id": tenant_id,
        "scope": filters.scope,
        "window_minutes": window_minutes,
        "cutoff": cutoff.isoformat() + "Z",
        "totals": {
            "events": total_events,
            "survey_responses": survey_responses,
            "survey_comments": survey_comments,
            "live_chat_comments": live_chat_comments,
        },
        "channels": top_channels,
        "events": top_events,
        "sentiment": sentiment,
        "geo": {
            "points": points[:2000],
            "hotspots": _build_hotspots([{"channel": e.channel, "metadata": e.metadata, "ts": e.ts} for e in events], limit=20),
        },
        "recommendations": [
            "Priorizar canales con mayor volumen para staffing en vivo.",
            "Cruzar comentarios de encuestas con eventos realtime para detectar quiebres UX.",
            "Activar alertas cuando suban eventos negativos por barrio/canal.",
        ],
    }


@admin_analytics_bp.get("/realtime-hub")
def admin_analytics_realtime_hub():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    window_minutes = request.args.get("window_minutes", 30)
    payload = _build_realtime_hub_payload(filters, window_minutes=window_minutes)
    return _json(payload)


@admin_analytics_bp.get("/export.csv")
def admin_analytics_export_csv():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    overview = get_summary(filters)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["metric", "value"])
    for key, value in overview.get("totals", {}).items():
        writer.writerow([key, value])

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=analytics_{filters.tenant_id}.csv"
        },
    )


@admin_analytics_bp.get("/export.pdf")
def admin_analytics_export_pdf():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    overview = get_summary(filters)

    lines = [
        "Reporte de analytics",
        f"Tenant: {filters.tenant_id}",
        f"Emitido: {datetime.utcnow().isoformat()}Z",
    ]
    for key, value in overview.get("totals", {}).items():
        lines.append(f"{key}: {value}")

    text = "\\n".join(lines).replace("(", "[").replace(")", "]")
    stream = f"BT /F1 12 Tf 50 780 Td ({text}) Tj ET".encode("latin-1", errors="replace")
    body = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    body += b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    body += b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    body += b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    body += f"5 0 obj<</Length {len(stream)}>>stream\n".encode("ascii") + stream + b"\nendstream endobj\n"
    body += b"xref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000118 00000 n \n0000000244 00000 n \n0000000314 00000 n \n"
    body += b"trailer<</Root 1 0 R/Size 6>>\nstartxref\n420\n%%EOF"

    return Response(
        body,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=analytics_{filters.tenant_id}.pdf"
        },
    )


@admin_analytics_bp.get("/dashboard")
def admin_analytics_dashboard():
    """Unified payload for the /analytics UI tabs (general/municipio/ventas/mapas)."""

    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    return _dashboard_response(filters)


@admin_analytics_bp.get("/hub")
def admin_analytics_hub():
    """Alias endpoint to support frontend convergence on one analytics hub route."""

    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    return _dashboard_response(filters)
