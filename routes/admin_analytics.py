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

from models import AnalyticsEventV2, EncComentario, EncEncuesta, EncRespuesta, MunicipioTicket, PymeTicket, TenantProfile, TicketComentario, db
from services.analytics import get_geo_heatmap, get_summary
from services.analytics.filters import parse_filters
from services.analytics.rbac import require_access
from services.tenant_ticket_scope import municipio_ticket_scope_filter
from utils.map_config import get_map_config

admin_analytics_bp = Blueprint("admin_analytics", __name__, url_prefix="/admin/analytics")

_DASHBOARD_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_CACHE_TTL_SECONDS = 20.0
_ANALYTICS_HUB_CONTRACT_VERSION = "2026-analytics-hub-v2"
_MAP_CONTRACT_VERSION = "2026.04-maplibre-v1"
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

_WHATSAPP_FUNNEL_STAGES: list[tuple[str, str]] = [
    ("whatsapp_portal_menu_opened", "Portal abierto"),
    ("whatsapp_catalog_viewed", "Catalogo visto"),
    ("whatsapp_cart_started", "Carrito iniciado"),
    ("whatsapp_checkout_previewed", "Checkout revisado"),
    ("whatsapp_checkout_session_created", "Sesion de pago creada"),
    ("whatsapp_payment_webview_opened", "Checkout seguro abierto"),
    ("whatsapp_payment_webhook_confirmed", "Pago confirmado por webhook"),
    ("whatsapp_order_confirmed", "Pedido confirmado"),
    ("whatsapp_video_handoff_shared", "Videollamada compartida"),
    ("realtime_session_created", "Sesión realtime creada"),
    ("realtime_business_action_executed", "Acción de negocio realtime"),
]
WHATSAPP_FUNNEL_CONTRACT_VERSION = "admin.analytics.whatsapp_funnel.v1"

_WHATSAPP_ATTRIBUTION_KEYS = ("source", "utm_source", "channel", "origin", "entrypoint")


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


def _build_whatsapp_funnel_payload(filters, *, window_minutes: int = 60) -> dict[str, Any]:
    tenant_id = _tenant_id_as_int(filters.tenant_id)
    try:
        window_minutes_int = max(1, int(window_minutes))
    except (TypeError, ValueError):
        window_minutes_int = 60
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes_int)

    query = AnalyticsEventV2.query.filter(
        AnalyticsEventV2.tenant_id == tenant_id,
        AnalyticsEventV2.ts >= cutoff,
        AnalyticsEventV2.event_name.in_([stage[0] for stage in _WHATSAPP_FUNNEL_STAGES]),
    )
    if filters.date_from:
        query = query.filter(AnalyticsEventV2.ts >= filters.date_from)
    if filters.date_to:
        query = query.filter(AnalyticsEventV2.ts <= filters.date_to)
    if filters.canales:
        query = query.filter(AnalyticsEventV2.channel.in_(filters.canales))
    events = query.with_entities(
        AnalyticsEventV2.event_name,
        AnalyticsEventV2.channel,
        AnalyticsEventV2.metadata_payload,
        AnalyticsEventV2.session_id,
    ).all()

    stage_counts = {event_name: 0 for event_name, _ in _WHATSAPP_FUNNEL_STAGES}
    unique_sessions_per_stage: dict[str, set[str]] = {
        event_name: set() for event_name, _ in _WHATSAPP_FUNNEL_STAGES
    }
    unique_contacts_per_stage: dict[str, set[str]] = {
        event_name: set() for event_name, _ in _WHATSAPP_FUNNEL_STAGES
    }

    filtered_events = []
    for row in events:
        event_name = str(row.event_name or "").strip()
        normalized_channel = str(getattr(row, "channel", "") or "").strip().lower()
        metadata = getattr(row, "metadata_payload", None) or {}
        metadata_values = []
        if isinstance(metadata, dict):
            for key in _WHATSAPP_ATTRIBUTION_KEYS:
                raw_value = metadata.get(key)
                if raw_value is None:
                    continue
                metadata_values.append(str(raw_value).strip().lower())
        is_whatsapp_attributed = (
            event_name.startswith("whatsapp_")
            or normalized_channel.startswith("whatsapp")
            or any("whatsapp" in value for value in metadata_values)
        )
        if not is_whatsapp_attributed:
            continue

        filtered_events.append(row)
        stage_counts[row.event_name] = stage_counts.get(row.event_name, 0) + 1
        if row.session_id:
            unique_sessions_per_stage.setdefault(row.event_name, set()).add(str(row.session_id))

        contact_key = None
        if isinstance(metadata, dict):
            raw_contact = metadata.get("contact_key")
            if raw_contact is not None:
                contact_key = str(raw_contact).strip()
        if contact_key:
            unique_contacts_per_stage.setdefault(row.event_name, set()).add(contact_key)

    ordered = []
    previous_value = None
    for event_name, label in _WHATSAPP_FUNNEL_STAGES:
        total = stage_counts.get(event_name, 0)
        sessions = len(unique_sessions_per_stage.get(event_name, set()))
        contacts = len(unique_contacts_per_stage.get(event_name, set()))
        conversion = None
        if previous_value is not None and previous_value > 0:
            conversion = round((sessions / previous_value) * 100, 2)
        ordered.append(
            {
                "event_name": event_name,
                "label": label,
                "total": total,
                "unique_sessions": sessions,
                "unique_contacts": contacts,
                "conversion_from_prev_pct": conversion,
            }
        )
        if sessions > 0:
            previous_value = sessions

    return {
        "tenant_id": filters.tenant_id,
        "scope": filters.scope,
        "window_minutes": window_minutes_int,
        "cutoff": cutoff.isoformat(),
        "contract_version": WHATSAPP_FUNNEL_CONTRACT_VERSION,
        "stages": ordered,
        "totals": {
            "events": len(filtered_events),
            "unique_sessions": len(
                {
                    str(row.session_id)
                    for row in filtered_events
                    if row.session_id
                }
            ),
            "unique_contacts": len(
                {
                    str((metadata or {}).get("contact_key")).strip()
                    for row in filtered_events
                    for metadata in [getattr(row, "metadata_payload", None)]
                    if isinstance(metadata, dict) and (metadata or {}).get("contact_key")
                }
            ),
        },
        "notes": [
            "Funnel orientado a flujos WhatsApp/widget: catalogo, carrito, checkout seguro, pago confirmado por webhook y seguimiento.",
            "El estado pagado se debe tomar del webhook del proveedor, no del retorno visual del checkout.",
            "Funnel orientado a flujos WhatsApp → portal/realtime.",
            "Usar junto con /admin/analytics/realtime-hub para contexto operativo.",
        ],
    }


def _etag_for_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _request_id() -> str:
    inbound = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return inbound or f"req_{uuid.uuid4().hex}"


def _pdf_escape(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_text_pdf(lines: list[str]) -> bytes:
    y_start = 790
    line_height = 14
    chunks = ["BT /F1 10 Tf"]
    current_y = y_start
    for index, line in enumerate(lines):
        safe_line = _pdf_escape(line)
        if index == 0:
            chunks.append(f"50 {current_y} Td ({safe_line}) Tj")
        else:
            chunks.append(f"0 -{line_height} Td ({safe_line}) Tj")
        current_y -= line_height
        if current_y <= 40:
            break
    chunks.append("ET")
    stream = "\n".join(chunks).encode("latin-1", errors="replace")

    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        f"<</Length {len(stream)}>>stream\n".encode("ascii") + stream + b"\nendstream",
    ]

    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")

    xref_start = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(f"trailer<</Root 1 0 R/Size {len(offsets)}>>\n".encode("ascii"))
    body.extend(f"startxref\n{xref_start}\n%%EOF".encode("ascii"))
    return bytes(body)


def _coerce_geo_limit(value: Any, *, default: int = 2000, min_value: int = 100, max_value: int = 10000) -> int:
    try:
        parsed = int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [item.strip() for item in value.split(",")]
    if len(parts) != 4:
        return None
    try:
        min_lng, min_lat, max_lng, max_lat = [float(item) for item in parts]
    except (TypeError, ValueError):
        return None
    if min_lng > max_lng or min_lat > max_lat:
        return None
    return min_lng, min_lat, max_lng, max_lat




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


def _extract_event_coordinates(event: dict[str, Any]) -> tuple[float, float] | None:
    md = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    lat = event.get("lat")
    lng = event.get("lng")
    if lat is None:
        lat = md.get("lat") if md.get("lat") is not None else md.get("latitude")
    if lng is None:
        lng = md.get("lng") if md.get("lng") is not None else md.get("lon")
    if lng is None:
        lng = md.get("longitude")
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def _build_heatmap_quality_contract(
    events: list[dict[str, Any]],
    *,
    visible_points: int,
    source_limit: int,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    total_events = len(events or [])
    geocoded_events = sum(1 for event in events if _extract_event_coordinates(event) is not None)
    missing_location_count = max(0, total_events - geocoded_events)
    coverage_rate = round(geocoded_events / total_events, 4) if total_events else 0.0

    if total_events == 0:
        state = "empty"
        reason_code = "no_events_for_filters"
        label = "Sin eventos para los filtros"
    elif geocoded_events == 0:
        state = "blocked"
        reason_code = "missing_coordinates"
        label = "Sin coordenadas reales"
    elif coverage_rate < 0.35:
        state = "partial"
        reason_code = "low_location_coverage"
        label = "Cobertura baja"
    else:
        state = "ready"
        reason_code = "ready"
        label = "Mapa confiable"

    return {
        "state": state,
        "label": label,
        "reason_code": reason_code,
        "coverage_rate": coverage_rate,
        "coverage_percent": round(coverage_rate * 100, 1),
        "total_events": total_events,
        "geocoded_events": geocoded_events,
        "visible_points": visible_points,
        "missing_location_count": missing_location_count,
        "source_limit": source_limit,
        "bbox_applied": list(bbox) if bbox else None,
        "can_render_heatmap": visible_points > 0,
        "empty_state_action": {
            "label": "Pedir ubicacion al vecino",
            "reason": "Los mapas premium dependen de coordenadas reales capturadas por WhatsApp, widget o geocodificacion.",
            "route": "/perfil?tab=tickets",
        },
    }


def _build_heatmap_realtime_contract(events: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [event.get("ts") for event in events if isinstance(event.get("ts"), datetime)]
    latest = max(timestamps) if timestamps else None
    return {
        "poll_seconds": 20,
        "socket_namespace": "analytics",
        "socket_events": ["analytics.event.created", "ticket.updated", "survey.vote.created"],
        "latest_event_at": _iso_z(latest),
        "live_sources": ["whatsapp", "widget", "admin", "survey", "voice"],
    }


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


def _build_maplibre_heatmap_layers(events: list[dict[str, Any]], *, style_url: str | None = None, source_limit: int = 2000, bbox: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}

    for event in events:
        md = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        lat = event.get("lat")
        lng = event.get("lng")
        if lat is None:
            lat = md.get("lat") if md.get("lat") is not None else md.get("latitude")
        if lng is None:
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

        if bbox and not (bbox[0] <= lng <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        bucket = by_category.setdefault(categoria, {"count": 0, "weight": 0.0, "points": []})
        bucket["count"] += 1
        bucket["weight"] += weight
        bucket["points"].append({"lat": lat, "lng": lng, "weight": round(weight, 4)})

    ranked = sorted(by_category.items(), key=lambda item: item[1]["weight"], reverse=True)
    feature_collection = {"type": "FeatureCollection", "features": []}
    min_ts: datetime | None = None
    max_ts: datetime | None = None

    source_events = events
    if source_limit and len(source_events) > source_limit:
        source_events = source_events[-source_limit:]

    for event in source_events:
        md = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        lat = event.get("lat")
        lng = event.get("lng")
        if lat is None:
            lat = md.get("lat") if md.get("lat") is not None else md.get("latitude")
        if lng is None:
            lng = md.get("lng") if md.get("lng") is not None else md.get("lon")
        if lng is None:
            lng = md.get("longitude")
        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            continue
        if bbox and not (bbox[0] <= lng <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        ts = event.get("ts")
        labels = _event_segment_labels(event)
        weight = _extract_vote_weight(md)
        if isinstance(ts, datetime):
            min_ts = ts if min_ts is None else min(min_ts, ts)
            max_ts = ts if max_ts is None else max(max_ts, ts)
            ts_value = int(ts.timestamp())
        else:
            ts_value = None
        feature_collection["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": {
                    "categoria": labels.get("categoria", "sin_dato"),
                    "barrio": labels.get("barrio", "sin_dato"),
                    "distrito": labels.get("distrito", "sin_dato"),
                    "canal": labels.get("canal", "sin_dato"),
                    "channel": labels.get("canal", "sin_dato"),
                    "weight": round(weight, 4),
                    "ts": ts_value,
                },
            }
        )

    resolved_style_url = style_url or "https://demotiles.maplibre.org/style.json"

    if not ranked:
        return {
            "provider": "maplibre",
            "engine": "maplibre-gl-js",
            "style_url": resolved_style_url,
            "contract_version": _MAP_CONTRACT_VERSION,
            "categories": [],
            "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": 0},
            "source": feature_collection,
            "source_meta": {"limit": source_limit, "bbox": list(bbox) if bbox else None, "total_input_events": len(events)},
            "source_options": {
                "cluster": True,
                "clusterMaxZoom": 14,
                "clusterRadius": 45,
            },
            "layers": {
                "heatmap": {"id": "events-heat", "type": "heatmap", "source": "events"},
                "clusters": {"id": "events-clusters", "type": "circle", "source": "events"},
                "points": {"id": "events-points", "type": "circle", "source": "events"},
            },
            "interactions": {
                "hover": True,
                "time_slider": {"enabled": False, "field": "ts"},
            },
            "telemetry": {
                "event_endpoint": "/api/analytics/event",
                "events": ["map_loaded", "layer_toggle", "time_slider_changed", "cluster_click"],
            },
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
        "provider": "maplibre",
        "engine": "maplibre-gl-js",
        "style_url": resolved_style_url,
        "contract_version": _MAP_CONTRACT_VERSION,
        "source": feature_collection,
        "source_meta": {"limit": source_limit, "bbox": list(bbox) if bbox else None, "total_input_events": len(events)},
        "source_options": {
            "cluster": True,
            "clusterMaxZoom": 14,
            "clusterRadius": 45,
        },
        "layers": {
            "heatmap": {
                "id": "events-heat",
                "source": "events",
                "type": "heatmap",
                "weight_field": "weight",
            },
            "clusters": {
                "id": "events-clusters",
                "source": "events",
                "type": "circle",
                "filter": ["has", "point_count"],
            },
            "points": {
                "id": "events-points",
                "source": "events",
                "type": "circle",
                "filter": ["!", ["has", "point_count"]],
                "color_by": "categoria",
            },
        },
        "categories": categories,
        "legend": {
            "mode": "category_weight",
            "min_weight": 0,
            "max_weight": round(max_weight, 4),
        },
        "interactions": {
            "hover": True,
            "time_slider": {
                "enabled": bool(min_ts and max_ts and min_ts != max_ts),
                "field": "ts",
                "from": min_ts.isoformat() if min_ts else None,
                "to": max_ts.isoformat() if max_ts else None,
                "step_minutes": 15,
            },
        },
        "telemetry": {
            "event_endpoint": "/api/analytics/event",
            "events": ["map_loaded", "layer_toggle", "time_slider_changed", "cluster_click"],
        },
    }


def _iso_z(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _geo_points_from_layers(geo_layers: dict[str, Any], *, limit: int = 2000) -> list[dict[str, Any]]:
    source = geo_layers.get("source") if isinstance(geo_layers, dict) else {}
    features = source.get("features") if isinstance(source, dict) else []
    points: list[dict[str, Any]] = []
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            lng = float(coordinates[0])
            lat = float(coordinates[1])
        except (TypeError, ValueError):
            continue
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        raw_weight = properties.get("count") or properties.get("weight") or 1
        try:
            count = max(1, int(round(float(raw_weight))))
        except (TypeError, ValueError):
            count = 1
        point = {
            "lat": lat,
            "lng": lng,
            "count": count,
        }
        channel = properties.get("channel") or properties.get("canal")
        if channel:
            point["channel"] = str(channel)
        categoria = properties.get("categoria")
        if categoria:
            point["categoria"] = str(categoria)
        points.append(point)
        if len(points) >= limit:
            break
    return points


def _compact_hotspots(hotspots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for hotspot in hotspots or []:
        if not isinstance(hotspot, dict):
            continue
        label = (
            hotspot.get("barrio")
            or hotspot.get("distrito")
            or hotspot.get("categoria")
            or hotspot.get("label")
        )
        if not label:
            continue
        compact.append({"label": str(label), "count": int(hotspot.get("count") or 0)})
    return compact


def _build_realtime_comments(
    *,
    tenant_id: int,
    cutoff: datetime,
    events: list[Any],
    limit: int = 20,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    tenant = db.session.get(TenantProfile, tenant_id)

    for row in events:
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        text = (
            metadata.get("comment")
            or metadata.get("comentario")
            or metadata.get("text")
            or metadata.get("message")
            or metadata.get("transcript")
        )
        if not text:
            continue
        sentiment = str(metadata.get("sentiment") or metadata.get("sentimiento") or "neutral").strip().lower()
        if sentiment not in {"positive", "neutral", "negative"}:
            sentiment = "neutral"
        comments.append(
            {
                "channel": str(row.channel or metadata.get("channel") or "event").strip().lower() or "event",
                "text": str(text).strip(),
                "created_at": _iso_z(row.ts),
                "sentiment": sentiment,
            }
        )

    survey_rows = (
        EncComentario.query.join(EncEncuesta, EncComentario.encuesta_id == EncEncuesta.id)
        .filter(EncEncuesta.tenant_id == tenant_id)
        .filter(EncComentario.created_at >= cutoff)
        .order_by(EncComentario.created_at.desc())
        .limit(limit)
        .all()
    )
    for comment in survey_rows:
        text = str(getattr(comment, "texto", "") or "").strip()
        if not text:
            continue
        comments.append(
            {
                "channel": "survey",
                "text": text,
                "created_at": _iso_z(getattr(comment, "created_at", None)),
                "sentiment": "neutral",
            }
        )

    ticket_rows = (
        TicketComentario.query
        .outerjoin(MunicipioTicket, TicketComentario.municipio_ticket_id == MunicipioTicket.id)
        .outerjoin(PymeTicket, TicketComentario.pyme_ticket_id == PymeTicket.id)
        .filter(TicketComentario.fecha >= cutoff)
        .filter(
            or_(
                municipio_ticket_scope_filter(tenant),
                PymeTicket.tenant_id == tenant_id,
            )
        )
        .order_by(TicketComentario.fecha.desc())
        .limit(limit)
        .all()
    )
    for comment in ticket_rows:
        text = str(getattr(comment, "comentario", "") or "").strip()
        if not text:
            continue
        comments.append(
            {
                "channel": "live_chat",
                "text": text,
                "created_at": _iso_z(getattr(comment, "fecha", None)),
                "sentiment": "neutral",
            }
        )

    comments.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return comments[:limit]


def _build_realtime_recommendations(
    *,
    top_channels: list[dict[str, Any]],
    top_events: list[dict[str, Any]],
    sentiment: dict[str, int],
    hotspots: list[dict[str, Any]],
) -> list[str]:
    recommendations: list[str] = []
    if top_channels:
        channel = top_channels[0].get("channel")
        count = int(top_channels[0].get("count") or 0)
        if channel and count:
            recommendations.append(f"Revisar capacidad del canal {channel}: concentra {count} eventos en la ventana.")
    if top_events:
        event = top_events[0].get("event")
        count = int(top_events[0].get("count") or 0)
        if event and count:
            recommendations.append(f"Auditar el flujo {event}: registra {count} ocurrencias recientes.")
    negative = int(sentiment.get("negative") or 0)
    positive = int(sentiment.get("positive") or 0)
    if negative and negative >= positive:
        recommendations.append(f"Atender comentarios negativos: {negative} eventos marcados como negative.")
    if hotspots:
        hotspot = hotspots[0]
        label = hotspot.get("label")
        count = int(hotspot.get("count") or 0)
        if label and count:
            recommendations.append(f"Analizar hotspot {label}: concentra {count} eventos geolocalizados o segmentados.")
    return recommendations


def _build_survey_operations_contract(
    *,
    survey_responses: int,
    survey_comments: int,
    event_counts: dict[str, int],
    window_minutes: int,
) -> dict[str, Any]:
    vote_events = sum(
        int(count or 0)
        for name, count in event_counts.items()
        if "survey" in str(name or "").lower() or "encuesta" in str(name or "").lower() or "vote" in str(name or "").lower()
    )
    engagement = int(survey_responses or 0) + int(survey_comments or 0) + vote_events
    if engagement > 0:
        status = "live"
        headline = f"{engagement} senales de participacion en los ultimos {window_minutes} minutos."
    else:
        status = "quiet"
        headline = "Sin actividad de encuestas en la ventana actual."

    next_actions: list[dict[str, str]] = [
        {
            "id": "open_surveys_admin",
            "label": "Abrir encuestas",
            "route": "/admin/encuestas",
        },
        {
            "id": "review_live_results",
            "label": "Revisar resultados en vivo",
            "route": "/admin/encuestas?focus=live",
        },
    ]
    if survey_comments:
        next_actions.insert(
            0,
            {
                "id": "moderate_comments",
                "label": "Moderar comentarios",
                "route": "/admin/encuestas?focus=comments",
            },
        )

    return {
        "contract_version": "analytics.survey_operations.v1",
        "status": status,
        "headline": headline,
        "window_minutes": window_minutes,
        "responses": int(survey_responses or 0),
        "comments": int(survey_comments or 0),
        "vote_events": vote_events,
        "engagement": engagement,
        "live_signal": engagement > 0,
        "recommended_actions": next_actions,
    }


def _realtime_hub_ui_contract() -> dict[str, Any]:
    return {
        "labels": {
            "tabs_realtime_hub": "Realtime Hub",
            "survey_ops_title": "Encuestas y votaciones en vivo",
            "survey_ops_quiet": "Sin actividad de encuestas en este periodo",
            "sections_map": "Mapa en tiempo real",
            "sections_segments": "Segmentos",
            "empty": "Sin datos para este periodo",
            "empty_map": "Sin puntos geograficos publicados",
            "applied_filters": "Filtros aplicados",
        }
    }


def _map_reading_contract(
    *,
    geo_layers: dict[str, Any],
    geo_points: list[dict[str, Any]],
    hotspots: list[dict[str, Any]] | None = None,
    route: str = "/perfil?tab=estadisticas",
) -> dict[str, Any]:
    features = ((geo_layers.get("source") or {}).get("features") or []) if isinstance(geo_layers, dict) else []
    point_count = len(geo_points or [])
    feature_count = len(features)
    total_points = feature_count or point_count
    legend = (geo_layers.get("legend") or {}) if isinstance(geo_layers, dict) else {}

    if total_points:
        headline = f"Mapa con {total_points} puntos geograficos reales en el periodo."
        empty_state = None
        recommended_action = {"label": "Ver mapa", "route": route}
        if hotspots:
            first = hotspots[0] or {}
            label = first.get("label") or first.get("barrio") or first.get("distrito") or first.get("categoria")
            count = first.get("count")
            if label and count:
                headline = f"{label} concentra {count} eventos en el periodo."
                recommended_action = {"label": "Analizar segmento", "route": route}
    else:
        headline = "Sin puntos geograficos reales para este periodo."
        empty_state = "Sin puntos geograficos publicados para los filtros actuales."
        recommended_action = {"label": "Cambiar filtros", "route": route}

    return {
        "headline": headline,
        "legend": legend or {"mode": "category_weight", "min_weight": 0, "max_weight": 0},
        "empty_state": empty_state,
        "recommended_action": recommended_action,
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
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    payload = _ensure_total_interactions(get_summary(filters))
    return _json(payload)


@admin_analytics_bp.get("/heatmap")
def admin_analytics_heatmap():
    request_id = _request_id()
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
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
        AnalyticsEventV2.lat.label("lat"),
        AnalyticsEventV2.lng.label("lng"),
    ).all()


    temporal = [
        {"weekday": int(row.weekday), "hour": int(row.hour), "count": int(row.total)}
        for row in temporal_rows
        if row.weekday is not None and row.hour is not None
    ]

    segment_events = [
        {"channel": row.channel, "metadata": row.metadata, "ts": row.ts, "lat": row.lat, "lng": row.lng}
        for row in events
    ]
    segment_filters = _extract_segment_filters()
    filtered_events = [event for event in segment_events if _event_matches_segment_filters(event, segment_filters)]

    map_cfg = get_map_config() or {}
    style_url = map_cfg.get("style_url") or "https://demotiles.maplibre.org/style.json"
    source_limit = _coerce_geo_limit(request.args.get("geo_limit"))
    bbox = _parse_bbox(request.args.get("bbox"))
    geo_layers = _build_maplibre_heatmap_layers(filtered_events, style_url=style_url, source_limit=source_limit, bbox=bbox)
    geo_points = _geo_points_from_layers(geo_layers, limit=source_limit)
    hotspots = _build_hotspots(filtered_events)
    map_reading = _map_reading_contract(geo_layers=geo_layers, geo_points=geo_points, hotspots=hotspots)
    quality = _build_heatmap_quality_contract(
        filtered_events,
        visible_points=len(geo_points),
        source_limit=source_limit,
        bbox=bbox,
    )
    realtime = _build_heatmap_realtime_contract(filtered_events)

    response = _json({
        "contract_version": "analytics.heatmap.v1",
        "request_id": request_id,
        "points": geo_points,
        "summary": {
            "points": len(geo_points),
            "events": len(filtered_events),
            "coverage_rate": quality["coverage_rate"],
            "coverage_percent": quality["coverage_percent"],
            "missing_location_count": quality["missing_location_count"],
            "quality_state": quality["state"],
        },
        "render_contract": {
            "state": "ready" if quality["can_render_heatmap"] else "empty",
            "map_engine": "maplibre-gl-js",
            "layers": ["heatmap", "clusters", "points"],
            "point_format": {"lat": "number", "lng": "number", "weight": "number"},
            "quality_state": quality["state"],
            "reason_code": quality["reason_code"],
        },
        "quality": quality,
        "realtime": realtime,
        **map_reading,
        "geo": base,
        "geo_layers": geo_layers,
        "temporal": temporal,
        "segments": _aggregate_heatmap_segments(filtered_events),
        "segments_filters_applied": {k: sorted(v) for k, v in segment_filters.items()},
        "period_comparison": _build_period_comparison(filtered_events),
        "hotspots": hotspots,
        "tz": tz,
    })
    response.headers.setdefault("X-Request-Id", request_id)
    return response





def _coerce_window_minutes(value: Any, *, default: int = 30) -> int:
    """Parse window_minutes defensively to avoid 500s on malformed query params."""

    try:
        parsed = int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        parsed = default
    return max(5, min(parsed, 24 * 60))

def _build_realtime_hub_payload(filters, *, window_minutes: int = 30) -> dict[str, Any]:
    tenant_id = _tenant_id_as_int(filters.tenant_id)
    tenant = db.session.get(TenantProfile, tenant_id)
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

    for row in events:
        ch = (row.channel or "unknown").lower()
        channel_counts[ch] = channel_counts.get(ch, 0) + 1
        ev = (row.event_name or "unknown").lower()
        event_counts[ev] = event_counts.get(ev, 0) + 1
        md = row.metadata if isinstance(row.metadata, dict) else {}
        sent = str(md.get("sentiment") or md.get("sentimiento") or "").lower()
        if sent in sentiment:
            sentiment[sent] += 1

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
                municipio_ticket_scope_filter(tenant),
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

    segment_events = [
        {"channel": row.channel, "metadata": row.metadata, "ts": row.ts, "lat": row.lat, "lng": row.lng}
        for row in events
    ]
    segment_filters = _extract_segment_filters()
    filtered_events = [event for event in segment_events if _event_matches_segment_filters(event, segment_filters)]

    map_cfg = get_map_config() or {}
    style_url = map_cfg.get("style_url") or "https://demotiles.maplibre.org/style.json"
    source_limit = _coerce_geo_limit(request.args.get("geo_limit"))
    bbox = _parse_bbox(request.args.get("bbox"))
    geo_layers = _build_maplibre_heatmap_layers(filtered_events, style_url=style_url, source_limit=source_limit, bbox=bbox)
    geo_points = _geo_points_from_layers(geo_layers, limit=source_limit)
    hotspots = _compact_hotspots(_build_hotspots(filtered_events, limit=20))
    comments = _build_realtime_comments(tenant_id=tenant_id, cutoff=cutoff, events=list(events), limit=20)
    map_reading = _map_reading_contract(
        geo_layers=geo_layers,
        geo_points=geo_points,
        hotspots=hotspots,
        route="/analytics?tab=realtime-hub",
    )

    recommendations = _build_realtime_recommendations(
        top_channels=top_channels,
        top_events=top_events,
        sentiment=sentiment,
        hotspots=hotspots,
    )
    survey_operations = _build_survey_operations_contract(
        survey_responses=survey_responses,
        survey_comments=survey_comments,
        event_counts=event_counts,
        window_minutes=window_minutes,
    )

    return {
        "contract_version": "analytics.realtime_hub.v1",
        "request_id": _request_id(),
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
        "top_channels": top_channels,
        "top_events": top_events,
        "channels": top_channels,
        "events": top_events,
        "sentiment": sentiment,
        "survey_operations": survey_operations,
        "comments": comments,
        "recommendations": recommendations,
        "hotspots": hotspots,
        "geo_points": geo_points,
        "geo_layers": geo_layers,
        **map_reading,
        "map": map_reading,
        "segments": _aggregate_heatmap_segments(filtered_events),
        "segments_filters_applied": {k: sorted(v) for k, v in segment_filters.items()},
        "ui": _realtime_hub_ui_contract(),
        "geo": {
            "points": geo_points,
            "hotspots": hotspots,
        },
    }


@admin_analytics_bp.get("/realtime-hub")
def admin_analytics_realtime_hub():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    window_minutes = request.args.get("window_minutes", 30)
    payload = _build_realtime_hub_payload(filters, window_minutes=window_minutes)
    response = _json(payload)
    response.headers.setdefault("X-Request-Id", payload.get("request_id") or _request_id())
    return response


@admin_analytics_bp.get("/whatsapp-funnel")
def admin_analytics_whatsapp_funnel():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    window_minutes = request.args.get("window_minutes", 60)
    payload = _build_whatsapp_funnel_payload(filters, window_minutes=window_minutes)
    return _json(payload)


@admin_analytics_bp.get("/export.csv")
def admin_analytics_export_csv():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
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
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    overview = get_summary(filters)

    tenant_id = _tenant_id_as_int(filters.tenant_id)
    query = AnalyticsEventV2.query.filter(AnalyticsEventV2.tenant_id == tenant_id)
    if filters.date_from:
        query = query.filter(AnalyticsEventV2.ts >= filters.date_from)
    if filters.date_to:
        query = query.filter(AnalyticsEventV2.ts <= filters.date_to)
    events = query.with_entities(
        AnalyticsEventV2.channel.label("channel"),
        AnalyticsEventV2.metadata_payload.label("metadata"),
        AnalyticsEventV2.ts.label("ts"),
    ).all()
    segment_events = [{"channel": row.channel, "metadata": row.metadata, "ts": row.ts} for row in events]
    segments = _aggregate_heatmap_segments(segment_events)
    hotspots = _build_hotspots(segment_events, limit=5)

    lines = [
        "Reporte de analytics",
        f"Tenant: {filters.tenant_id}",
        f"Scope: {filters.scope}",
        f"Emitido: {datetime.utcnow().isoformat()}Z",
        "",
        "Totales:",
    ]
    for key, value in sorted((overview.get("totals") or {}).items(), key=lambda item: item[0]):
        lines.append(f"- {key}: {value}")

    lines.extend(["", "Segmentacion principal:"])
    for label in ("categoria", "sexo", "rango_edad", "canal"):
        top = (segments.get(label) or [])[:3]
        rendered = ", ".join(f"{item['label']} ({item['count']})" for item in top) if top else "sin datos"
        lines.append(f"- {label}: {rendered}")

    lines.extend(["", "Hotspots:"])
    if hotspots:
        for item in hotspots:
            lines.append(
                f"- {item.get('categoria')} | {item.get('barrio')} | {item.get('distrito')} => {item.get('count')}"
            )
    else:
        lines.append("- Sin hotspots en el rango seleccionado")

    body = _build_simple_text_pdf(lines)

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
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    return _dashboard_response(filters)


@admin_analytics_bp.get("/hub")
def admin_analytics_hub():
    """Alias endpoint to support frontend convergence on one analytics hub route."""

    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    return _dashboard_response(filters)
