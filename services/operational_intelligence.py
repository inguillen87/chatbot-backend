from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
import json
from urllib.parse import quote

from sqlalchemy import and_, case, func, or_

from models import (
    AnalyticsEventV2,
    ChatSessionContext,
    EncEncuesta,
    EncRespuesta,
    MarketOrder,
    MunicipioTicket,
    Order,
    PedidoConversacional,
    PublicSurvey,
    PublicSurveyResponse,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TicketRealtimeState,
    User,
)
from services.commerce_unified import dedupe_unified_orders
from services.employee_ticket_access import apply_employee_ticket_category_scope
from services.huggingface_ai_insights import build_collection_ai_insights, build_map_ai_layers
from services.operational_heatmap_access import (
    build_employee_aggregated_heatmap,
    employee_heatmap_scope_empty,
    filter_ticket_records_for_heatmap,
    is_employee_heatmap_viewer,
    privileged_heatmap_privacy,
)
from services.survey_response_provenance import (
    SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    SURVEY_RESPONSE_ORIGIN_REAL,
    SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
    build_survey_response_provenance,
)
from services.tenant_ticket_scope import scoped_municipio_ticket_query
from services.territorial_evidence import (
    build_territorial_facets,
    canonicalize_territorial_category,
    coordinate_jurisdiction_disposition,
    coordinate_jurisdiction_status,
    explicit_zone,
    extract_location_evidence,
    normalize_address_corridor_key,
    normalize_address_key,
    resolve_tenant_jurisdiction,
)
from services.territorial_geocoding import (
    build_territorial_geocoding_candidate,
    discover_territorial_geocoding_candidates,
)


_CLOSED_STATES = {"cerrado", "closed", "resuelto", "resolved", "finalizado", "done"}
_OVERDUE_STATES = {"vencido", "overdue", "breached"}
_SLA_AT_RISK_STATES = {"at_risk", "risk", "warning", "en_riesgo"}
_SLA_HEALTHY_STATES = {"ok", "normal", "healthy", "on_track", "within_sla"}
_SLA_PAUSED_STATES = {"paused", "pausado", "on_hold", "waiting_customer", "esperando_cliente"}
_SLA_AT_RISK_WINDOW_SECONDS = 4 * 60 * 60
_ACTIVE_PRESENCE = {"active", "online", "typing", "present"}
_LIVE_SURVEY_STATES = {"publicada", "published", "activa", "active", "en_vivo", "live"}
_COMMERCE_REQUEST_KINDS = {
    "pedido",
    "order",
    "compra",
    "order_note",
    "quote_request",
    "handwritten_order",
    "document_order",
    "marketplace_order",
    "receipt",
    "tax_bill",
    "certificate",
    "service_request",
    "other",
    "municipal_service_request",
    "certificate_or_procedure_review",
    "tax_or_payment_support",
}

_EMPLOYEE_SCOPE_UNAVAILABLE_REASON = "employee_category_boundary_unavailable"
_TICKET_SOURCE_MODEL_BY_RECORD_SOURCE = {
    "tenant_ticket": "TenantTicket",
    "municipio_ticket": "MunicipioTicket",
    "pyme_ticket": "PymeTicket",
}


def _operations_scope_contract(*, employee_view: bool) -> dict[str, Any]:
    if not employee_view:
        return {
            "mode": "tenant_wide",
            "category_scoped": False,
            "scoped_sources": [
                "tickets",
                "surveys",
                "chats",
                "commerce",
                "employees",
                "live_chat",
                "maps.heatmap",
            ],
            "unavailable_sources": [],
        }
    return {
        "mode": "employee_category_limited",
        "category_scoped": True,
        "scoped_sources": [
            "tickets",
            "queue_truth",
            "live_chat",
            "maps.heatmap",
        ],
        "unavailable_sources": [
            "surveys",
            "chats",
            "commerce",
            "employees",
        ],
        "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
        "notice": (
            "Las fuentes sin frontera de categoria para empleados se omiten; "
            "no representan conteos cero."
        ),
    }


def _employee_unavailable_section(
    *,
    contract_version: str,
    summary: dict[str, Any],
    **empty_fields: Any,
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "available": False,
        "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
        "summary": summary,
        **empty_fields,
    }


def _employee_unavailable_survey_metrics() -> dict[str, Any]:
    return _employee_unavailable_section(
        contract_version="operations.surveys.v1",
        summary={
            "encuestas": 0,
            "public_surveys": 0,
            "active": 0,
            "votaciones_live": 0,
            "responses": 0,
            "responses_with_geo": 0,
            "public_responses": 0,
        },
        by_channel=[],
        live_items=[],
        live_control_room={
            "contract_version": "operations.survey_live_control_room.v1",
            "enabled": False,
            "state": "scope_unavailable",
            "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
            "summary": {
                "live_surveys": 0,
                "responses": 0,
                "responses_with_geo": 0,
                "geo_coverage_rate": 0.0,
                "channels": [],
            },
            "monitors": [],
            "actions": [],
        },
        response_provenance={
            "available": False,
            "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
        },
    )


def _employee_unavailable_chat_metrics() -> dict[str, Any]:
    return _employee_unavailable_section(
        contract_version="operations.chats.v1",
        summary={
            "events": 0,
            "messages": 0,
            "sessions": 0,
            "whatsapp_messages": 0,
            "widget_messages": 0,
            "handoffs": 0,
            "handoff_rate": 0.0,
        },
        by_channel=[],
        by_event=[],
    )


def _employee_unavailable_commerce_metrics() -> dict[str, Any]:
    return _employee_unavailable_section(
        contract_version="operations.commerce.v1",
        summary={
            "orders": 0,
            "source_records": 0,
            "deduplicated_mirrors": 0,
            "assisted_orders": 0,
            "orders_needing_review": 0,
            "detected_items": 0,
            "matched_items": 0,
            "unmatched_items": 0,
            "review_rate": 0.0,
            "total_monetary": None,
            "currency": None,
            "currencies": 0,
        },
        by_state=[],
        by_origin=[],
        by_source_model=[],
        by_request_kind=[],
        totals_by_currency=[],
        review_items=[],
    )


def _employee_unavailable_employee_metrics() -> dict[str, Any]:
    return _employee_unavailable_section(
        contract_version="operations.employees.v1",
        summary={
            "employees": 0,
            "assigned_open_tickets": 0,
            "coverage_rate": None,
            "unassigned_open_tickets": 0,
        },
        items=[],
        coverage={
            "categories": [],
            "channels": [],
            "uncovered_categories": [],
            "uncovered_channels": [],
        },
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _norm(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    return text or default


def _counter(counter: Counter, *, limit: int = 12) -> list[dict[str, Any]]:
    return [{"key": key, "label": key, "count": int(count)} for key, count in counter.most_common(limit)]


def _between(query, column, start_date: datetime, end_date: datetime):
    return query.filter(column >= start_date, column <= end_date)


def _tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "plan": tenant.plan,
    }


def _period_delta(start_date: datetime, end_date: datetime) -> timedelta:
    delta = end_date - start_date
    if delta.total_seconds() <= 0:
        return timedelta(days=7)
    return delta


def _percent_change(current: float | int, previous: float | int) -> float | None:
    current_value = float(current or 0)
    previous_value = float(previous or 0)
    if previous_value == 0:
        return None if current_value == 0 else 100.0
    return round(((current_value - previous_value) / previous_value) * 100, 2)


def _trend_direction(current: float | int, previous: float | int) -> str:
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def _trend_item(key: str, current: float | int, previous: float | int) -> dict[str, Any]:
    return {
        "key": key,
        "current": current,
        "previous": previous,
        "direction": _trend_direction(current, previous),
        "percent_change": _percent_change(current, previous),
    }


def _priority_weight(priority: str, status: str) -> float:
    priority_score = {"low": 0.8, "baja": 0.8, "normal": 1.0, "medium": 1.0, "media": 1.0, "high": 1.4, "alta": 1.4, "urgent": 1.8, "critica": 1.8}
    status_score = 1.5 if status in _OVERDUE_STATES else 1.0
    return round(priority_score.get(_norm(priority, "normal"), 1.0) * status_score, 2)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _record_has_coordinates(record: dict[str, Any]) -> bool:
    return record.get("lat") is not None and record.get("lng") is not None


def _record_address_from_metadata(metadata: dict[str, Any]) -> str | None:
    # Fail closed for nested payloads.  The previous implementation converted
    # ``{"location": {...}}`` into a Python dict string and published that as
    # an address.  The evidence extractor accepts only scalar addresses and
    # traverses structured location objects explicitly.
    return extract_location_evidence(("ticket_metadata", metadata)).get("address")


def _record_zone_from_metadata(metadata: dict[str, Any]) -> str | None:
    """Return only an explicitly declared territorial label.

    Street addresses remain useful geocoding inputs, but they are not zones.
    Keeping the two concepts separate prevents a private address from becoming
    a misleading segment or an apparent official boundary in the heatmap.
    """

    return extract_location_evidence(("ticket_metadata", metadata)).get("zone")


def _ticket_location_evidence(
    *,
    lat: Any,
    lng: Any,
    address: Any = None,
    zone: Any = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return extract_location_evidence(
        (
            "ticket_columns",
            {
                "latitud": lat,
                "longitud": lng,
                "direccion": address,
                "zona": zone,
            },
        ),
        ("ticket_metadata", metadata or {}),
    )


def _normalize_gender(value: Any) -> str:
    raw = _norm(value, "unknown")
    mapping = {
        "f": "femenino",
        "female": "femenino",
        "fem": "femenino",
        "mujer": "femenino",
        "femenino": "femenino",
        "m": "masculino",
        "male": "masculino",
        "masc": "masculino",
        "hombre": "masculino",
        "masculino": "masculino",
        "nb": "no_binario",
        "non_binary": "no_binario",
        "no_binario": "no_binario",
        "no binario": "no_binario",
    }
    return mapping.get(raw, raw if raw else "unknown")


def _age_range(age: Any, explicit_range: Any = None, birth_year: Any = None) -> str:
    explicit = _norm(explicit_range, "")
    if explicit:
        return explicit
    try:
        age_int = int(age) if age not in (None, "") else None
    except (TypeError, ValueError):
        age_int = None
    if age_int is None and birth_year not in (None, ""):
        try:
            age_int = datetime.now(timezone.utc).year - int(birth_year)
        except (TypeError, ValueError):
            age_int = None
    if age_int is None:
        return "unknown"
    if age_int < 18:
        return "menor_18"
    if age_int < 25:
        return "18_24"
    if age_int < 35:
        return "25_34"
    if age_int < 45:
        return "35_44"
    if age_int < 60:
        return "45_59"
    return "60_plus"


def _demographics_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    profile = _json_object(metadata.get("demographics")) or _json_object(metadata.get("perfil_demografico"))
    merged = {**metadata, **profile}
    age = _first_value(merged, "edad", "age")
    gender = _first_value(merged, "genero", "gender", "sexo")
    age_range = _age_range(age, _first_value(merged, "rango_edad", "age_range", "rango_etario"), _first_value(merged, "anio_nacimiento", "birth_year"))
    return {
        "gender": _normalize_gender(gender),
        "age": int(age) if str(age or "").strip().isdigit() else None,
        "age_range": age_range,
        "source": "metadata" if gender is not None or age is not None or age_range != "unknown" else "missing",
    }


def _segment_items(counter: Counter, *, limit: int = 20) -> list[dict[str, Any]]:
    total = sum(int(value or 0) for value in counter.values())
    items = []
    for key, count in counter.most_common(limit):
        value = int(count or 0)
        items.append(
            {
                "key": key,
                "label": key,
                "count": value,
                "share": round((value / total) * 100, 2) if total else 0.0,
            }
        )
    return items


def _normalize_filter_values(values: Any) -> set[str]:
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",")]
    if not isinstance(values, list):
        return set()
    return {_norm(item, "") for item in values if _norm(item, "")}


def _normalize_age_range_filter_values(values: Any) -> set[str]:
    normalized = set()
    for value in _normalize_filter_values(values):
        normalized.add(_age_range(value) if value.isdigit() else value)
    return normalized


def _normalized_heatmap_filter_values(key: str, values: Any) -> set[str]:
    normalized = _normalize_filter_values(values)
    if key == "category":
        return {
            canonicalize_territorial_category(value)["category"]
            for value in normalized
        }
    if key == "age_range":
        return _normalize_age_range_filter_values(values)
    if key == "address":
        return {item for value in normalized if (item := normalize_address_key(value))}
    if key == "corridor":
        return {
            item
            for value in normalized
            if (item := normalize_address_corridor_key(value))
        }
    return normalized


def _employee_safe_applied_filters(filters: dict[str, list[str]]) -> dict[str, list[str]]:
    """Do not echo exact address/corridor query values in employee payloads."""

    safe = {key: list(values) for key, values in (filters or {}).items()}
    for key in ("address", "corridor"):
        if safe.get(key):
            safe[key] = ["private_filter_applied"]
    return safe


def _point_matches_filters(point: dict[str, Any], filters: dict[str, Any]) -> bool:
    filter_map = {
        "category": point.get("category"),
        "gender": point.get("gender"),
        "age_range": point.get("age_range"),
        "source": point.get("source"),
        "channel": point.get("channel"),
        "status": point.get("status"),
        "zone": point.get("zone"),
        "sla_state": point.get("sla_state"),
        "assignee_id": point.get("assignee_id"),
        "address": point.get("address"),
    }
    for key, raw_values in (filters or {}).items():
        allowed = _normalized_heatmap_filter_values(key, raw_values)
        if not allowed:
            continue
        if key == "address":
            if normalize_address_key(filter_map.get("address")) not in allowed:
                return False
            continue
        if key == "corridor":
            if normalize_address_corridor_key(filter_map.get("address")) not in allowed:
                return False
            continue
        if key == "source":
            source_allowed = set(allowed)
            aliases = {
                "tickets": "ticket",
                "surveys": "survey",
                "analytics_events": "analytics_event",
                "events": "analytics_event",
                "orders": "commerce",
                "commerce_activity": "commerce",
            }
            source_allowed.update(aliases.get(value, value) for value in allowed)
            if _norm(filter_map.get(key), "") not in source_allowed:
                return False
            continue
        if _norm(filter_map.get(key), "") not in allowed:
            return False
    return True


_GOVERNMENT_TENANT_TYPES = {
    "municipio",
    "municipalidad",
    "gobierno",
    "government",
    "provincia",
    "province",
    "ente_publico",
    "public_sector",
}


def _tenant_requires_verified_jurisdiction(tenant: TenantProfile) -> bool:
    return _norm(getattr(tenant, "tipo", None), "") in _GOVERNMENT_TENANT_TYPES


def _jurisdiction_has_verified_containment(jurisdiction: dict[str, Any] | None) -> bool:
    contract = jurisdiction or {}
    authority = _as_dict(contract.get("boundary_authority"))
    return bool(
        contract.get("enforced") is True
        and contract.get("containment_verified") is True
        and _norm(contract.get("containment_method"), "") == "point_in_polygon"
        and _norm(authority.get("kind"), "") == "official"
        and authority.get("source_ref")
        and authority.get("snapshot_sha256")
    )


def _attach_point_jurisdiction_evidence(
    point: dict[str, Any],
    jurisdiction: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind renderable coordinates to the exact boundary snapshot used.

    The point is never moved or recomputed here.  Missing authority remains
    explicit so a frontend can fail closed instead of inheriting trust from a
    visually adjacent global panel.
    """

    contract = jurisdiction or {}
    authority = _as_dict(contract.get("boundary_authority"))
    coordinate_status = _norm(
        point.get("coordinate_jurisdiction_status"), "missing"
    )
    verified = bool(
        _jurisdiction_has_verified_containment(contract)
        and coordinate_status == "within"
    )
    source_ref = authority.get("source_ref") if verified else None
    snapshot_sha256 = authority.get("snapshot_sha256") if verified else None
    point.update(
        {
            "containment_verified": verified,
            "source_ref": source_ref,
            "snapshot_sha256": snapshot_sha256,
            "jurisdiction_evidence": {
                "contract_version": "operations.point_jurisdiction_evidence.v1",
                "containment_verified": verified,
                "coordinate_jurisdiction_status": coordinate_status,
                "containment_method": (
                    contract.get("containment_method") if verified else None
                ),
                "authority_kind": authority.get("kind") if verified else None,
                "source_ref": source_ref,
                "snapshot_sha256": snapshot_sha256,
            },
        }
    )
    return point


def _jurisdiction_status_allows_map(
    status: str,
    *,
    require_verified_jurisdiction: bool,
    jurisdiction_verified: bool,
) -> bool:
    return coordinate_jurisdiction_disposition(
        status,
        require_verified_jurisdiction=require_verified_jurisdiction,
        jurisdiction_verified=jurisdiction_verified,
    ) == "allowed"


def _jurisdiction_exclusion_bucket(
    status: str,
    *,
    require_verified_jurisdiction: bool,
    jurisdiction_verified: bool,
) -> str | None:
    disposition = coordinate_jurisdiction_disposition(
        status,
        require_verified_jurisdiction=require_verified_jurisdiction,
        jurisdiction_verified=jurisdiction_verified,
    )
    return None if disposition == "allowed" else disposition


def _jurisdiction_review_reason_code(
    status: str,
    *,
    require_verified_jurisdiction: bool,
    jurisdiction_verified: bool,
) -> str | None:
    exclusion_bucket = _jurisdiction_exclusion_bucket(
        status,
        require_verified_jurisdiction=require_verified_jurisdiction,
        jurisdiction_verified=jurisdiction_verified,
    )
    if exclusion_bucket == "unverified":
        return "official_jurisdiction_boundary_unavailable"
    if exclusion_bucket == "outside" and jurisdiction_verified:
        return "coordinates_outside_verified_jurisdiction"
    if exclusion_bucket == "outside":
        return "coordinates_outside_configured_jurisdiction"
    return None


def _point_matches_bbox(point: dict[str, Any], bbox: dict[str, float] | None) -> bool:
    if not bbox:
        return True
    try:
        lat = float(point.get("lat"))
        lng = float(point.get("lng"))
    except (TypeError, ValueError):
        return False
    return (
        float(bbox["south"]) <= lat <= float(bbox["north"])
        and float(bbox["west"]) <= lng <= float(bbox["east"])
    )


def _geojson_point_feature(point: dict[str, Any], *, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    props = dict(properties or point)
    props.pop("lat", None)
    props.pop("lng", None)
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(point["lng"]), float(point["lat"])],
        },
        "properties": props,
    }


def _geojson_feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _heatmap_source_quality(
    *,
    points: list[dict[str, Any]],
    records: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    commerce_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_counts = Counter(point.get("source") or "unknown" for point in points)
    ticket_records = [
        record
        for record in records
        if record.get("source") in {"tenant_ticket", "municipio_ticket", "pyme_ticket"}
    ]
    ticket_points = source_counts.get("ticket", 0)
    ticket_total = len(ticket_records)
    commerce_total = len(commerce_records or [])
    commerce_points = int(source_counts.get("commerce", 0))

    sources = {
        "ticket": {
            "label": "Reclamos",
            "records": ticket_total,
            "points": int(ticket_points),
            "pending_geocode": len(geocoding_candidates),
            "coordinate_coverage_pct": round((ticket_points / ticket_total) * 100, 2) if ticket_total else 0.0,
        },
        "survey": {
            "label": "Encuestas y votaciones",
            "records": int(source_counts.get("survey", 0)),
            "points": int(source_counts.get("survey", 0)),
            "pending_geocode": 0,
            "coordinate_coverage_pct": 100.0 if source_counts.get("survey", 0) else 0.0,
        },
        "analytics_event": {
            "label": "Eventos digitales",
            "records": int(source_counts.get("analytics_event", 0)),
            "points": int(source_counts.get("analytics_event", 0)),
            "pending_geocode": 0,
            "coordinate_coverage_pct": 100.0 if source_counts.get("analytics_event", 0) else 0.0,
        },
        "commerce": {
            "label": "Pedidos y ventas",
            "records": commerce_total,
            "points": commerce_points,
            "pending_geocode": 0,
            "coordinate_coverage_pct": round((commerce_points / commerce_total) * 100, 2) if commerce_total else 0.0,
            "privacy_mode": "coordinates_without_customer_pii",
        },
    }

    return {
        "contract_version": "operations.heatmap_source_quality.v1",
        "sources": sources,
        "summary": {
            "records": sum(item["records"] for item in sources.values()),
            "points": len(points),
            "pending_geocode": len(geocoding_candidates),
            "weakest_source": min(
                sources.items(),
                key=lambda item: (item[1]["coordinate_coverage_pct"], -item[1]["pending_geocode"]),
            )[0],
        },
    }


def _heatmap_geo_layers(
    *,
    points: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    hotspots: list[dict[str, Any]],
    category_layers: list[dict[str, Any]],
    boundaries: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category_feature_collections = {
        str(layer.get("key") or "unknown"): _geojson_feature_collection(
            [_geojson_point_feature(point) for point in layer.get("points") or []]
        )
        for layer in category_layers
    }
    return {
        "contract_version": "operations.heatmap_geo_layers.v1",
        "provider": "geojson",
        "coordinate_order": "lng_lat",
        **({"boundaries": boundaries} if isinstance(boundaries, dict) else {}),
        "points": _geojson_feature_collection([_geojson_point_feature(point) for point in points]),
        "cells": _geojson_feature_collection([_geojson_point_feature(cell) for cell in cells]),
        "hotspots": _geojson_feature_collection([_geojson_point_feature(hotspot) for hotspot in hotspots]),
        "categories": category_feature_collections,
    }


def _heatmap_map_layers(
    *,
    geo_layers: dict[str, Any],
    category_layers: list[dict[str, Any]],
    source_quality: dict[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": "operations.heatmap_map_layers.v1",
        "engine": "maplibre",
        "format": "geojson",
        "default_layers": ["base_heatmap", "cells", "hotspots"],
        "layers": [
            {
                "id": "base_heatmap",
                "label": "Actividad territorial",
                "source": "geo_layers.points",
                "type": "heatmap",
                "weight_field": "weight",
                "available": bool((geo_layers.get("points") or {}).get("features")),
            },
            {
                "id": "cells",
                "label": "Celdas operativas",
                "source": "geo_layers.cells",
                "type": "circle",
                "weight_field": "count",
                "available": bool((geo_layers.get("cells") or {}).get("features")),
            },
            {
                "id": "hotspots",
                "label": "Zonas criticas",
                "source": "geo_layers.hotspots",
                "type": "symbol",
                "weight_field": "operational_score",
                "available": bool((geo_layers.get("hotspots") or {}).get("features")),
            },
            {
                "id": "category_layers",
                "label": "Categorias",
                "source": "geo_layers.categories",
                "type": "heatmap_collection",
                "available_categories": [item.get("key") for item in category_layers],
                "available": bool(category_layers),
            },
            {
                "id": "commerce_activity",
                "label": "Pedidos y ventas",
                "source": "geo_layers.points",
                "source_filter": {"source": "commerce"},
                "type": "heatmap",
                "weight_field": "weight",
                "available": bool(
                    ((source_quality.get("sources") or {}).get("commerce") or {}).get("points")
                ),
                "privacy_mode": "coordinates_without_customer_pii",
            },
        ],
        "telemetry": {
            "event_endpoint": "/api/analytics/event",
            "events": ["map_layer_toggled", "heatmap_hotspot_selected", "heatmap_bbox_changed"],
        },
        "source_quality": source_quality,
    }


def _sla_observation(
    *,
    status: str,
    metadata: dict[str, Any] | None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return the evidence-backed SLA state for one current ticket.

    Missing SLA evidence is deliberately ``unknown``.  A missing field must
    never become a synthetic healthy/normal result in an operational KPI.
    """

    details = _as_dict(metadata)
    sla = _as_dict(details.get("sla"))
    raw_state = _norm(
        sla.get("status")
        or sla.get("state")
        or details.get("sla_status")
        or details.get("sla_state"),
        "",
    )
    due_raw = (
        sla.get("resolution_due_at")
        or sla.get("next_update_due_at")
        or sla.get("due_at")
        or details.get("resolution_due_at")
        or details.get("next_update_due_at")
        or details.get("sla_due_at")
        or details.get("due_at")
    )
    due_at = _parse_datetime(due_raw)
    observed_at = _aware_datetime(as_of) or datetime.now(timezone.utc)
    paused = bool(
        status in _SLA_PAUSED_STATES
        or sla.get("paused_at")
        or sla.get("is_paused") is True
        or raw_state in _SLA_PAUSED_STATES
    )
    eligible = status not in _CLOSED_STATES and not paused

    evidence: list[str] = []
    if due_at is not None:
        evidence.append("due_at")
    if raw_state:
        evidence.append("explicit_state")
    if status in _OVERDUE_STATES:
        evidence.append("ticket_status")

    known = bool(
        eligible
        and (
            due_at is not None
            or raw_state in (_OVERDUE_STATES | _SLA_AT_RISK_STATES | _SLA_HEALTHY_STATES)
            or status in _OVERDUE_STATES
        )
    )
    breached = bool(
        known
        and (
            status in _OVERDUE_STATES
            or raw_state in _OVERDUE_STATES
            or (due_at is not None and due_at <= observed_at)
        )
    )
    seconds_to_due = None
    if due_at is not None:
        seconds_to_due = int((due_at - observed_at).total_seconds())
    at_risk = bool(
        known
        and not breached
        and (
            raw_state in _SLA_AT_RISK_STATES
            or (
                seconds_to_due is not None
                and 0 < seconds_to_due <= _SLA_AT_RISK_WINDOW_SECONDS
            )
        )
    )
    if not eligible:
        state = "not_eligible"
    elif not known:
        state = "unknown"
    elif breached:
        state = "breached"
    elif at_risk:
        state = "at_risk"
    else:
        state = "healthy"

    return {
        "eligible": eligible,
        "known": known,
        "state": state,
        "breached": breached,
        "at_risk": at_risk,
        "due_at": _iso(due_at),
        "seconds_to_due": seconds_to_due,
        "evidence": evidence,
        "raw_state": raw_state or None,
    }


def _tenant_ticket_record(ticket: TenantTicket, *, as_of: datetime | None = None) -> dict[str, Any]:
    extra = _as_dict(ticket.datos_extra)
    location = _ticket_location_evidence(
        lat=ticket.latitud,
        lng=ticket.longitud,
        metadata=extra,
    )
    status = _norm(ticket.estado, "nuevo")
    sla = _sla_observation(status=status, metadata=extra, as_of=as_of)
    priority = _norm(extra.get("priority") or extra.get("prioridad"), "normal")
    channel = _norm(extra.get("channel") or extra.get("canal") or ticket.origen, "web")
    category = canonicalize_territorial_category(ticket.categoria)
    return {
        "source": "tenant_ticket",
        "source_model": "TenantTicket",
        "id": ticket.id,
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "status": status,
        "priority": priority,
        "channel": channel,
        "category": category["category"],
        "raw_category": category["raw_category"],
        "category_provenance": category["provenance"],
        "category_id": getattr(ticket, "categoria_id", None),
        "assignee_id": extra.get("assignee_id"),
        "zone": _norm(location.get("zone"), "sin_zona"),
        "address": location.get("address"),
        "reported_location_text": location.get("reported_location_text"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "location_provenance": location.get("provenance"),
        "demographics": _demographics_from_metadata(extra),
        "created_at": getattr(ticket, "created_at", None),
        "updated_at": getattr(ticket, "updated_at", None),
        "sla": sla,
        "sla_state": sla["state"],
        "overdue": sla["breached"],
    }


def _municipio_ticket_record(ticket: MunicipioTicket, *, as_of: datetime | None = None) -> dict[str, Any]:
    status = _norm(ticket.estado, "nuevo")
    channel = _norm(getattr(ticket, "canal_ingreso", None), "web")
    details = _json_object(getattr(ticket, "detalles", None))
    extra = _as_dict(getattr(ticket, "datos_extra", None))
    metadata = {**details, **extra}
    location = _ticket_location_evidence(
        lat=ticket.latitud,
        lng=ticket.longitud,
        address=getattr(ticket, "direccion", None),
        zone=getattr(ticket, "distrito", None),
        metadata=metadata,
    )
    sla = _sla_observation(status=status, metadata=metadata, as_of=as_of)
    category = canonicalize_territorial_category(ticket.categoria)
    return {
        "source": "municipio_ticket",
        "source_model": "MunicipioTicket",
        "id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Reclamo {ticket.nro_ticket}",
        "status": status,
        "priority": "normal",
        "channel": channel,
        "category": category["category"],
        "raw_category": category["raw_category"],
        "category_provenance": category["provenance"],
        "category_id": getattr(ticket, "categoria_id", None),
        "assignee_id": getattr(ticket, "asignado_a_id", None),
        "zone": _norm(location.get("zone"), "sin_zona"),
        "address": location.get("address"),
        "reported_location_text": location.get("reported_location_text"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "location_provenance": location.get("provenance"),
        "demographics": _demographics_from_metadata(metadata),
        "created_at": ticket.fecha,
        "updated_at": ticket.ultima_actividad or ticket.fecha,
        "sla": sla,
        "sla_state": sla["state"],
        "overdue": sla["breached"],
    }


def _pyme_ticket_record(ticket: PymeTicket, *, as_of: datetime | None = None) -> dict[str, Any]:
    status = _norm(ticket.estado, "nuevo")
    extra = _as_dict(getattr(ticket, "datos_extra", None))
    location = _ticket_location_evidence(
        lat=getattr(ticket, "latitud", None),
        lng=getattr(ticket, "longitud", None),
        address=getattr(ticket, "direccion", None),
        metadata=extra,
    )
    sla = _sla_observation(
        status=status,
        metadata=extra,
        as_of=as_of,
    )
    category = canonicalize_territorial_category(ticket.categoria)
    return {
        "source": "pyme_ticket",
        "source_model": "PymeTicket",
        "id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Ticket {ticket.nro_ticket}",
        "status": status,
        "priority": "normal",
        "channel": "web",
        "category": category["category"],
        "raw_category": category["raw_category"],
        "category_provenance": category["provenance"],
        "category_id": getattr(ticket, "categoria_id", None),
        "assignee_id": getattr(ticket, "asignado_a_id", None),
        "zone": _norm(location.get("zone"), "sin_zona"),
        "address": location.get("address"),
        "reported_location_text": location.get("reported_location_text"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "location_provenance": location.get("provenance"),
        "demographics": {"gender": "unknown", "age": None, "age_range": "unknown", "source": "missing"},
        "created_at": ticket.fecha,
        "updated_at": ticket.fecha,
        "sla": sla,
        "sla_state": sla["state"],
        "overdue": sla["breached"],
    }


def _collect_ticket_records(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    as_of: datetime | None = None,
    viewer: Any = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    tenant_ticket_query = _between(
        TenantTicket.query.filter_by(tenant_id=tenant.id),
        TenantTicket.created_at,
        start_date,
        end_date,
    )
    tenant_ticket_query = apply_employee_ticket_category_scope(tenant_ticket_query, viewer, TenantTicket)
    tenant_tickets = tenant_ticket_query.all()
    records.extend(_tenant_ticket_record(ticket, as_of=as_of) for ticket in tenant_tickets)

    municipio_ticket_query = _between(_municipio_ticket_query(tenant), MunicipioTicket.fecha, start_date, end_date)
    municipio_ticket_query = apply_employee_ticket_category_scope(municipio_ticket_query, viewer, MunicipioTicket)
    municipio_tickets = municipio_ticket_query.all()
    records.extend(_municipio_ticket_record(ticket, as_of=as_of) for ticket in municipio_tickets)

    pyme_ticket_query = _between(
        PymeTicket.query.filter_by(tenant_id=tenant.id),
        PymeTicket.fecha,
        start_date,
        end_date,
    )
    pyme_ticket_query = apply_employee_ticket_category_scope(pyme_ticket_query, viewer, PymeTicket)
    pyme_tickets = pyme_ticket_query.all()
    records.extend(_pyme_ticket_record(ticket, as_of=as_of) for ticket in pyme_tickets)

    return records


def _open_status_query(query, status_column):
    normalized_status = func.lower(func.trim(func.coalesce(status_column, "")))
    return query.filter(~normalized_status.in_(_CLOSED_STATES))


def _created_at_membership_query(query, created_column, *, as_of: datetime):
    """Apply the queue's declared creation-membership boundary."""

    return query.filter(or_(created_column.is_(None), created_column <= as_of))


def _collect_open_ticket_records(
    tenant: TenantProfile,
    *,
    as_of: datetime,
    viewer: Any = None,
) -> list[dict[str, Any]]:
    """Collect the complete current queue without a created-at window."""

    records: list[dict[str, Any]] = []
    tenant_ticket_query = _created_at_membership_query(
        _open_status_query(
            TenantTicket.query.filter_by(tenant_id=tenant.id),
            TenantTicket.estado,
        ),
        TenantTicket.created_at,
        as_of=as_of,
    )
    tenant_ticket_query = apply_employee_ticket_category_scope(tenant_ticket_query, viewer, TenantTicket)
    records.extend(_tenant_ticket_record(ticket, as_of=as_of) for ticket in tenant_ticket_query.all())

    municipio_ticket_query = _created_at_membership_query(
        _open_status_query(
            _municipio_ticket_query(tenant),
            MunicipioTicket.estado,
        ),
        MunicipioTicket.fecha,
        as_of=as_of,
    )
    municipio_ticket_query = apply_employee_ticket_category_scope(municipio_ticket_query, viewer, MunicipioTicket)
    records.extend(_municipio_ticket_record(ticket, as_of=as_of) for ticket in municipio_ticket_query.all())

    pyme_ticket_query = _created_at_membership_query(
        _open_status_query(
            PymeTicket.query.filter_by(tenant_id=tenant.id),
            PymeTicket.estado,
        ),
        PymeTicket.fecha,
        as_of=as_of,
    )
    pyme_ticket_query = apply_employee_ticket_category_scope(pyme_ticket_query, viewer, PymeTicket)
    records.extend(_pyme_ticket_record(ticket, as_of=as_of) for ticket in pyme_ticket_query.all())
    return records


def _queue_membership_quality(
    tenant: TenantProfile,
    *,
    as_of: datetime,
    included_records: list[dict[str, Any]],
    viewer: Any = None,
) -> dict[str, Any]:
    """Report records quarantined by the queue creation-time boundary."""

    source_queries = (
        (
            "TenantTicket",
            _open_status_query(
                TenantTicket.query.filter_by(tenant_id=tenant.id),
                TenantTicket.estado,
            ),
            TenantTicket.created_at,
            TenantTicket,
        ),
        (
            "MunicipioTicket",
            _open_status_query(
                _municipio_ticket_query(tenant),
                MunicipioTicket.estado,
            ),
            MunicipioTicket.fecha,
            MunicipioTicket,
        ),
        (
            "PymeTicket",
            _open_status_query(
                PymeTicket.query.filter_by(tenant_id=tenant.id),
                PymeTicket.estado,
            ),
            PymeTicket.fecha,
            PymeTicket,
        ),
    )
    future_by_source = [
        {
            "source_model": source_model,
            "excluded_records": int(
                apply_employee_ticket_category_scope(query, viewer, ticket_model)
                .filter(created_column > as_of)
                .count()
            ),
        }
        for source_model, query, created_column, ticket_model in source_queries
    ]
    future_total = sum(item["excluded_records"] for item in future_by_source)
    null_created_at = len(
        [record for record in included_records if _aware_datetime(record.get("created_at")) is None]
    )
    return {
        "contract_version": "operations.queue_membership_quality.v1",
        "creation_membership": "created_at_null_or_lte_as_of",
        "null_created_at": {
            "policy": "included_with_unknown_age",
            "included_records": null_created_at,
        },
        "future_created_at": {
            "state": "quarantined" if future_total else "clean",
            "policy": "excluded_from_queue",
            "excluded_records": future_total,
            "by_source_model": future_by_source,
        },
    }


def _municipio_ticket_query(tenant: TenantProfile):
    return scoped_municipio_ticket_query(tenant)


def _ticket_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(record["status"] for record in records)
    channel_counts = Counter(record["channel"] for record in records)
    category_counts = Counter(record["category"] for record in records)
    priority_counts = Counter(record["priority"] for record in records)
    source_counts = Counter(record["source"] for record in records)
    open_records = [record for record in records if record["status"] not in _CLOSED_STATES]
    overdue_records = [record for record in records if record.get("overdue")]

    return {
        "summary": {
            "total": len(records),
            "open": len(open_records),
            "closed": len(records) - len(open_records),
            "overdue": len(overdue_records),
            "unassigned": len([record for record in open_records if not record.get("assignee_id")]),
            "whatsapp": len([record for record in records if record["channel"] == "whatsapp"]),
            "with_location": len([record for record in records if record.get("lat") is not None and record.get("lng") is not None]),
        },
        "by_status": _counter(status_counts),
        "by_channel": _counter(channel_counts),
        "by_category": _counter(category_counts),
        "by_priority": _counter(priority_counts),
        "by_source": _counter(source_counts),
    }


def _percentage(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _build_queue_truth(
    *,
    queue_records: list[dict[str, Any]],
    period_records: list[dict[str, Any]],
    start_date: datetime,
    end_date: datetime,
    as_of: datetime,
    membership_quality: dict[str, Any],
) -> dict[str, Any]:
    """Separate point-in-time queue truth from tickets created in a period."""

    source_model_names = _TICKET_SOURCE_MODEL_BY_RECORD_SOURCE
    source_counts = Counter(record.get("source") for record in queue_records)
    period_source_counts = Counter(record.get("source") for record in period_records)

    sla_rows = [_as_dict(record.get("sla")) for record in queue_records]
    sla_eligible = len([row for row in sla_rows if row.get("eligible")])
    sla_known = len([row for row in sla_rows if row.get("eligible") and row.get("known")])
    sla_unknown = max(sla_eligible - sla_known, 0)
    sla_breached = len([row for row in sla_rows if row.get("eligible") and row.get("breached")])
    sla_at_risk = len([row for row in sla_rows if row.get("eligible") and row.get("at_risk")])
    sla_non_eligible = len(queue_records) - sla_eligible

    assigned = len([record for record in queue_records if record.get("assignee_id") is not None])
    unassigned = len(queue_records) - assigned
    owner_counts = Counter(
        str(record.get("assignee_id"))
        for record in queue_records
        if record.get("assignee_id") is not None
    )

    age_specs = [
        ("lt_1h", "Menos de 1 hora", 0, 60 * 60),
        ("1h_4h", "1 a 4 horas", 60 * 60, 4 * 60 * 60),
        ("4h_24h", "4 a 24 horas", 4 * 60 * 60, 24 * 60 * 60),
        ("1d_3d", "1 a 3 dias", 24 * 60 * 60, 3 * 24 * 60 * 60),
        ("3d_7d", "3 a 7 dias", 3 * 24 * 60 * 60, 7 * 24 * 60 * 60),
        ("gte_7d", "7 dias o mas", 7 * 24 * 60 * 60, None),
    ]
    age_counts = Counter()
    age_values: list[int] = []
    age_unknown = 0
    for record in queue_records:
        created_at = _aware_datetime(record.get("created_at"))
        if created_at is None:
            age_unknown += 1
            continue
        age_seconds = max(0, int((as_of - created_at).total_seconds()))
        age_values.append(age_seconds)
        for key, _label, lower, upper in age_specs:
            if age_seconds >= lower and (upper is None or age_seconds < upper):
                age_counts[key] += 1
                break

    age_buckets = [
        {
            "key": key,
            "label": label,
            "count": int(age_counts.get(key, 0)),
            "lower_bound_seconds": lower,
            "upper_bound_seconds": upper,
            "href": _crm_tickets_href(focus=f"open_age_{key}"),
            "link_semantics": "navigation_only",
            "exact_filter": False,
        }
        for key, label, lower, upper in age_specs
    ]
    if age_unknown:
        age_buckets.append(
            {
                "key": "unknown",
                "label": "Antiguedad desconocida",
                "count": age_unknown,
                "lower_bound_seconds": None,
                "upper_bound_seconds": None,
                "href": _crm_tickets_href(focus="open_age_unknown"),
                "link_semantics": "navigation_only",
                "exact_filter": False,
            }
        )

    source_coverage = [
        {
            "source_model": model_name,
            "open_records": int(source_counts.get(source_key, 0)),
        }
        for source_key, model_name in source_model_names.items()
    ]
    period_source_breakdown = [
        {
            "source_model": model_name,
            "created_records": int(period_source_counts.get(source_key, 0)),
        }
        for source_key, model_name in source_model_names.items()
    ]
    coverage = {
        "source_records": len(queue_records),
        "source_models": source_coverage,
        "tenant_scope": "authoritative",
        "sla": {
            "eligible": sla_eligible,
            "known": sla_known,
            "unknown": sla_unknown,
            "non_eligible": sla_non_eligible,
            "known_pct": _percentage(sla_known, sla_eligible),
        },
        "age": {
            "known": len(age_values),
            "unknown": age_unknown,
            "known_pct": _percentage(len(age_values), len(queue_records)),
        },
        "ownership": {
            "known": len(queue_records),
            "unknown": 0,
            "known_pct": 100.0 if queue_records else None,
        },
    }

    period_metrics = _ticket_metrics(period_records)
    return {
        "contract_version": "operations.queue_truth.v1",
        "grain": "one_current_open_ticket",
        "source_models": list(source_model_names.values()),
        "as_of": _iso(as_of),
        "membership_quality": membership_quality,
        "coverage": coverage,
        "queue_snapshot": {
            "grain": "one_current_open_ticket",
            "as_of": _iso(as_of),
            "summary": {
                "open_total": len(queue_records),
                "oldest_open_age_seconds": max(age_values) if age_values else None,
                "sla_breached": sla_breached,
                "sla_at_risk": sla_at_risk,
                "sla_unknown": sla_unknown,
                "assigned": assigned,
                "unassigned": unassigned,
            },
            "sla": {
                "eligible": sla_eligible,
                "known": sla_known,
                "unknown": sla_unknown,
                "non_eligible": sla_non_eligible,
                "breached": sla_breached,
                "at_risk": sla_at_risk,
                "healthy": max(sla_known - sla_breached - sla_at_risk, 0),
                "numerator": sla_breached,
                "denominator": sla_known,
                "breach_rate_pct": _percentage(sla_breached, sla_known),
                "at_risk_window_seconds": _SLA_AT_RISK_WINDOW_SECONDS,
                "state": (
                    "empty"
                    if sla_eligible == 0
                    else "unavailable"
                    if sla_known == 0
                    else "partial"
                    if sla_unknown > 0
                    else "available"
                ),
                "unknown_reason": "missing_sla_evidence" if sla_unknown else None,
            },
            "ownership": {
                "assigned": assigned,
                "unassigned": unassigned,
                "numerator": assigned,
                "denominator": len(queue_records),
                "assignment_rate_pct": _percentage(assigned, len(queue_records)),
                "by_owner": [
                    {
                        "assignee_id": owner_id,
                        "count": int(count),
                        "href": _crm_tickets_href(focus="assigned_open_queue", assignee=owner_id),
                        "link_semantics": "navigation_only",
                        "exact_filter": False,
                    }
                    for owner_id, count in owner_counts.most_common(20)
                ],
                "unassigned_href": _crm_tickets_href(
                    focus="unassigned_open_queue",
                    assignee="unassigned",
                ),
            },
            "age_buckets": age_buckets,
            "links": {
                "open": _crm_tickets_href(focus="open_queue"),
                "sla_breached": _crm_tickets_href(focus="sla_breached_queue"),
                "sla_at_risk": _crm_tickets_href(focus="sla_at_risk_queue"),
                "sla_unknown": _crm_tickets_href(focus="sla_unknown_queue"),
                "unassigned": _crm_tickets_href(
                    focus="unassigned_open_queue",
                    assignee="unassigned",
                ),
            },
            "link_contract": {
                "open": {"semantics": "navigation_only", "exact_filter": False},
                "sla_breached": {"semantics": "navigation_only", "exact_filter": False},
                "sla_at_risk": {"semantics": "navigation_only", "exact_filter": False},
                "sla_unknown": {"semantics": "navigation_only", "exact_filter": False},
                "unassigned": {"semantics": "navigation_only", "exact_filter": False},
                "ownership_by_owner": {"semantics": "navigation_only", "exact_filter": False},
                "age_buckets": {"semantics": "navigation_only", "exact_filter": False},
                "reason_code": "operational_queue_v1_not_yet_bound_to_queue_truth_snapshot",
                "notice": (
                    "Los drilldowns exactos están pendientes; estos contadores no abren filtros "
                    "hasta vincular la bandeja al mismo corte y alcance."
                ),
            },
        },
        "period_flow": {
            "grain": "one_ticket_created_in_period",
            "period": {"from": _iso(start_date), "to": _iso(end_date)},
            "as_of": _iso(as_of),
            "summary": {
                "created_total": period_metrics["summary"]["total"],
                "currently_open": period_metrics["summary"]["open"],
                "currently_closed": period_metrics["summary"]["closed"],
            },
            "by_source_model": period_source_breakdown,
            "status_semantics": "current_status_as_of_for_tickets_created_in_period",
            "does_not_measure": ["tickets_closed_in_period", "historical_backlog_snapshot"],
        },
    }


def _survey_metrics(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    encuestas_query = EncEncuesta.query.filter_by(tenant_id=tenant.id)
    survey_count = encuestas_query.count()
    live_vote_predicate = or_(
        EncEncuesta.es_votacion_envivo.is_(True),
        func.lower(func.coalesce(EncEncuesta.tipo, "")).like("%vot%"),
        func.lower(func.coalesce(EncEncuesta.titulo, "")).like("%vot%"),
    )
    live_vote_query = encuestas_query.filter(live_vote_predicate)
    live_vote_count = live_vote_query.count()
    live_votaciones = live_vote_query.order_by(EncEncuesta.id.asc()).limit(10).all()
    active_survey_count = encuestas_query.filter(
        func.lower(func.coalesce(EncEncuesta.estado, "")).in_(_LIVE_SURVEY_STATES)
    ).count()

    period_response_query = _between(
        EncRespuesta.query.filter_by(tenant_id=tenant.id),
        EncRespuesta.submitted_at,
        start_date,
        end_date,
    )
    real_period_response_query = period_response_query.filter(
        EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL
    )
    real_response_count = real_period_response_query.count()
    synthetic_response_count = period_response_query.filter(
        EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
    ).count()
    unverified_response_count = period_response_query.filter(
        EncRespuesta.response_origin
        == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
    ).count()
    response_with_geo_count = real_period_response_query.filter(
        EncRespuesta.lat.isnot(None),
        EncRespuesta.lng.isnot(None),
    ).count()

    responses_by_channel: Counter = Counter()
    for channel, count in (
        real_period_response_query.with_entities(
            EncRespuesta.canal,
            func.count(EncRespuesta.id),
        )
        .group_by(EncRespuesta.canal)
        .all()
    ):
        responses_by_channel[_norm(channel, "unknown")] += int(count or 0)

    response_by_survey: Counter = Counter()
    geo_by_survey: Counter = Counter()
    channel_by_survey: dict[int, Counter] = {}
    live_survey_ids = [int(encuesta.id) for encuesta in live_votaciones]
    if live_survey_ids:
        geo_condition = and_(
            EncRespuesta.lat.isnot(None),
            EncRespuesta.lng.isnot(None),
        )
        live_rows = (
            real_period_response_query.filter(
                EncRespuesta.encuesta_id.in_(live_survey_ids)
            )
            .with_entities(
                EncRespuesta.encuesta_id,
                EncRespuesta.canal,
                func.count(EncRespuesta.id),
                func.sum(case((geo_condition, 1), else_=0)),
            )
            .group_by(EncRespuesta.encuesta_id, EncRespuesta.canal)
            .all()
        )
        for survey_id, channel, count, geo_count in live_rows:
            normalized_survey_id = int(survey_id or 0)
            normalized_count = int(count or 0)
            response_by_survey[normalized_survey_id] += normalized_count
            geo_by_survey[normalized_survey_id] += int(geo_count or 0)
            channel_by_survey.setdefault(normalized_survey_id, Counter())[
                _norm(channel, "unknown")
            ] += normalized_count

    public_survey_count = PublicSurvey.query.filter_by(tenant_id=tenant.id).count()
    public_response_count = _between(
        PublicSurveyResponse.query.join(
            PublicSurvey,
            PublicSurveyResponse.survey_id == PublicSurvey.id,
        ).filter(PublicSurvey.tenant_id == tenant.id),
        PublicSurveyResponse.created_at,
        start_date,
        end_date,
    ).count()

    live_control_room = _survey_live_control_room(
        tenant=tenant,
        live_votaciones=live_votaciones,
        responses_by_channel=responses_by_channel,
        response_by_survey=response_by_survey,
        geo_by_survey=geo_by_survey,
        channel_by_survey=channel_by_survey,
    )

    return {
        "summary": {
            "encuestas": survey_count,
            "public_surveys": public_survey_count,
            "active": active_survey_count,
            "votaciones_live": live_vote_count,
            "responses": real_response_count + public_response_count,
            "responses_with_geo": response_with_geo_count,
            "public_responses": public_response_count,
        },
        "by_channel": _counter(responses_by_channel),
        "live_items": [
            {
                "id": encuesta.id,
                "slug": encuesta.slug,
                "title": encuesta.titulo,
                "status": encuesta.estado,
                "show_live_results": bool(encuesta.mostrar_resultados_envivo),
                "public_token": _public_survey_token(encuesta),
                "live_results_endpoint": f"/api/v2/public/surveys/{_public_survey_token(encuesta)}/live-results",
                "public_url": f"/e/{_public_survey_token(encuesta)}",
            }
            for encuesta in live_votaciones[:10]
        ],
        "live_control_room": live_control_room,
        "response_provenance": build_survey_response_provenance(
            real_count=real_response_count + public_response_count,
            synthetic_count=synthetic_response_count,
            unverified_count=unverified_response_count,
            mode="real",
        ),
    }


def _public_survey_token(encuesta: EncEncuesta) -> str:
    for link in getattr(encuesta, "links", []) or []:
        slug_publico = str(getattr(link, "slug_publico", "") or "").strip()
        if slug_publico:
            return slug_publico
    return str(getattr(encuesta, "slug", "") or "").strip()


def _survey_live_control_room(
    *,
    tenant: TenantProfile,
    live_votaciones: list[EncEncuesta],
    responses_by_channel: Counter,
    response_by_survey: Counter,
    geo_by_survey: Counter,
    channel_by_survey: dict[int, Counter],
) -> dict[str, Any]:
    monitors: list[dict[str, Any]] = []
    for encuesta in live_votaciones[:10]:
        survey_id = int(encuesta.id)
        public_token = _public_survey_token(encuesta)
        total = int(response_by_survey.get(survey_id, 0))
        geo = int(geo_by_survey.get(survey_id, 0))
        show_live_results = bool(encuesta.mostrar_resultados_envivo)
        published = _norm(encuesta.estado, "") in _LIVE_SURVEY_STATES
        monitors.append(
            {
                "id": survey_id,
                "slug": encuesta.slug,
                "public_token": public_token,
                "title": encuesta.titulo,
                "status": encuesta.estado,
                "type": encuesta.tipo,
                "live": bool(encuesta.es_votacion_envivo),
                "published": published,
                "show_live_results": show_live_results,
                "responses": total,
                "responses_with_geo": geo,
                "geo_coverage_rate": round((geo / total) * 100, 2) if total else 0.0,
                "channels": _counter(channel_by_survey.get(survey_id, Counter())),
                "public_url": f"/e/{public_token}",
                "admin_url": f"/admin/encuestas/{survey_id}/analytics?focus=live",
                "live_results_endpoint": f"/api/v2/public/surveys/{public_token}/live-results",
                "heatmap_endpoint": f"/api/v2/public/surveys/{public_token}/live-results?include_heatmap=1",
                "whatsapp_template_id": "gov_survey_invite" if getattr(tenant, "tipo", "") == "municipio" else "survey_invite",
                "state": (
                    "live_collecting"
                    if published and show_live_results
                    else "published_hidden_results"
                    if published
                    else "setup_required"
                ),
            }
        )

    total_responses = sum(int(item["responses"]) for item in monitors)
    total_geo = sum(int(item["responses_with_geo"]) for item in monitors)
    return {
        "contract_version": "operations.survey_live_control_room.v1",
        "enabled": bool(monitors),
        "state": "live" if any(item["state"] == "live_collecting" for item in monitors) else "setup_required" if monitors else "empty",
        "summary": {
            "live_surveys": len(monitors),
            "responses": total_responses,
            "responses_with_geo": total_geo,
            "geo_coverage_rate": round((total_geo / total_responses) * 100, 2) if total_responses else 0.0,
            "channels": _counter(responses_by_channel),
        },
        "monitors": monitors,
        "realtime": {
            "enabled": True,
            "refresh_seconds": 10 if monitors else 30,
            "socket_events": ["survey.vote.created", "survey.response.created", "analytics.event.created"],
            "fallback_polling": True,
        },
        "actions": [
            {
                "id": "open_surveys_admin",
                "label": "Abrir encuestas",
                "endpoint": "/api/v2/surveys",
                "route": "/admin/encuestas",
            },
            {
                "id": "open_operations_heatmap",
                "label": "Ver mapa operativo",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "route": "/analytics?tab=operations",
            },
            {
                "id": "sync_whatsapp_survey_template",
                "label": "Preparar plantilla WhatsApp",
                "endpoint": "/api/admin/templates/twilio-content/sync",
                "template_id": "gov_survey_invite" if getattr(tenant, "tipo", "") == "municipio" else "survey_invite",
            },
        ],
        "frontend_contract": {
            "render_as": "survey_live_control_room",
            "recommended_widgets": ["live_vote_cards", "channel_mix", "heatmap_coverage", "whatsapp_template_action"],
            "empty_state": "show_create_or_publish_survey_cta",
        },
    }


def _chat_metrics(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    events = _between(AnalyticsEventV2.query.filter_by(tenant_id=tenant.id), AnalyticsEventV2.ts, start_date, end_date).all()
    message_events = [event for event in events if _norm(event.event_name, "") in {"message_in", "message_out", "chat_message", "whatsapp_message"}]
    handoff_events = [event for event in events if "handoff" in _norm(event.event_name, "")]
    sessions = _between(ChatSessionContext.query.filter_by(tenant_id=tenant.id), ChatSessionContext.last_updated, start_date, end_date).all()

    by_channel = Counter(_norm(event.channel, "unknown") for event in message_events)
    by_event = Counter(_norm(event.event_name, "unknown") for event in events)
    whatsapp_messages = sum(count for channel, count in by_channel.items() if "whatsapp" in channel)
    widget_messages = sum(count for channel, count in by_channel.items() if "widget" in channel or channel == "web")

    return {
        "summary": {
            "events": len(events),
            "messages": len(message_events),
            "sessions": len(sessions),
            "whatsapp_messages": whatsapp_messages,
            "widget_messages": widget_messages,
            "handoffs": len(handoff_events),
            "handoff_rate": round((len(handoff_events) / len(message_events)) * 100, 2) if message_events else 0.0,
        },
        "by_channel": _counter(by_channel),
        "by_event": _counter(by_event),
    }


def _order_is_assisted(order: PedidoConversacional, metadata: dict[str, Any]) -> bool:
    contract_version = _norm(metadata.get("contract_version"), "")
    assisted_contract = _norm(metadata.get("assisted_request_contract_version"), "")
    origin = _norm(getattr(order, "origen", None), "")
    tipo = _norm(getattr(order, "tipo", None), "")
    return (
        contract_version == "marketplace.assisted_request.v1"
        or assisted_contract == "marketplace.assisted_request.v1"
        or "assisted" in origin
        or "marketplace_upload" in origin
        or "whatsapp_assisted" in origin
        or "nota" in tipo
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _commerce_request_kind(value: Any, fallback: str = "pedido") -> str:
    normalized = _norm(value, "")
    return normalized if normalized in _COMMERCE_REQUEST_KINDS else fallback


def _commerce_currency(value: Any, fallback: str = "UNKNOWN") -> str:
    normalized = str(value or "").strip().upper()
    return normalized or fallback


def _coordinates_from_payload(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, dict):
        return None, None

    lat = _float_or_none(_first_value(value, "lat", "latitude", "latitud"))
    lng = _float_or_none(_first_value(value, "lng", "lon", "longitude", "longitud"))
    if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng

    for key in (
        "coordinates",
        "coordenadas",
        "location",
        "ubicacion",
        "delivery",
        "delivery_address",
        "shipping_address",
        "destination",
        "destino",
    ):
        lat, lng = _coordinates_from_payload(value.get(key))
        if lat is not None and lng is not None:
            return lat, lng
    return None, None


def _commerce_record_location(record: Any, metadata: dict[str, Any]) -> tuple[float | None, float | None]:
    if isinstance(record, PymePedido):
        lat = _float_or_none(record.latitud)
        lng = _float_or_none(record.longitud)
        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    if isinstance(record, Order):
        return _coordinates_from_payload(record.delivery_address)
    return _coordinates_from_payload(metadata)


def _commerce_safe_record(record: Any) -> dict[str, Any]:
    if isinstance(record, PedidoConversacional):
        metadata = _order_metadata(record)
        needs_review, review_payload = _order_needs_operator_review(record)
        source = _json_object(metadata.get("source"))
        first_item = record.items[0] if record.items and isinstance(record.items[0], dict) else {}
        currency = (
            metadata.get("currency")
            or metadata.get("moneda")
            or source.get("currency")
            or source.get("moneda")
            or first_item.get("currency")
            or first_item.get("moneda")
        )
        lat, lng = _commerce_record_location(record, metadata)
        return {
            "id": f"conversational:{record.id}",
            "source_model": "PedidoConversacional",
            "source_id": record.id,
            "status": _norm(record.estado, "unknown"),
            "channel": _norm(record.origen, "unknown"),
            "request_kind": _commerce_request_kind(metadata.get("request_kind") or record.tipo),
            "total": _float_or_none(record.monto_monetario) or 0.0,
            "currency": _commerce_currency(currency),
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "metadata": metadata,
            "external_refs": {
                "mp_preference_id": record.mp_preference_id,
                "mp_payment_id": record.mp_payment_id,
            },
            "needs_review": needs_review,
            "review_summary": review_payload.get("summary") or {},
            "assisted": _order_is_assisted(record, metadata),
            "location": {"lat": lat, "lng": lng},
        }

    if isinstance(record, PymePedido):
        metadata = {
            "idempotency_key": record.idempotency_key,
            "tenant_id": record.tenant_id,
            "pyme_id": record.pyme_id,
        }
        lat, lng = _commerce_record_location(record, metadata)
        return {
            "id": f"legacy:{record.id}",
            "source_model": "PymePedido",
            "source_id": record.id,
            "status": _norm(record.estado, "unknown"),
            "channel": "whatsapp",
            "request_kind": "pedido",
            "total": _float_or_none(record.monto_total) or 0.0,
            "currency": "ARS",
            "created_at": _iso(record.fecha),
            "updated_at": _iso(record.fecha),
            "metadata": metadata,
            "external_refs": {
                "nro_pedido": record.nro_pedido,
                "idempotency_key": record.idempotency_key,
            },
            "needs_review": False,
            "review_summary": {},
            "assisted": str(record.idempotency_key or "").startswith("conv_order_"),
            "location": {"lat": lat, "lng": lng},
        }

    if isinstance(record, MarketOrder):
        metadata = _as_dict(record.metadata_payload)
        lat, lng = _commerce_record_location(record, metadata)
        return {
            "id": f"market:{record.id}",
            "source_model": "MarketOrder",
            "source_id": record.id,
            "status": _norm(record.status, "unknown"),
            "channel": _norm(record.channel, "web"),
            "request_kind": "marketplace_order",
            "total": _float_or_none(record.total_monetary) or 0.0,
            "currency": _commerce_currency(record.currency),
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "metadata": metadata,
            "external_refs": {
                "provider": record.external_provider,
                "order_id": record.external_order_id,
            },
            "needs_review": False,
            "review_summary": {},
            "assisted": bool(metadata.get("source_conversational_id")),
            "location": {"lat": lat, "lng": lng},
        }

    metadata = {"delivery_address": _as_dict(record.delivery_address)}
    lat, lng = _commerce_record_location(record, metadata)
    return {
        "id": f"order:{record.id}",
        "source_model": "Order",
        "source_id": record.id,
        "status": _norm(record.status, "unknown"),
        "channel": _norm(record.channel, "web_widget"),
        "request_kind": "order",
        "total": _float_or_none(record.total) or 0.0,
        "currency": _commerce_currency(record.currency),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "metadata": metadata,
        "external_refs": {},
        "needs_review": False,
        "review_summary": {},
        "assisted": False,
        "location": {"lat": lat, "lng": lng},
    }


def _collect_commerce_records(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
) -> tuple[list[dict[str, Any]], int]:
    records: list[Any] = []
    legacy_scope = PymePedido.tenant_id == tenant.id
    if tenant.pyme_id:
        legacy_scope = or_(
            legacy_scope,
            (PymePedido.tenant_id.is_(None)) & (PymePedido.pyme_id == tenant.pyme_id),
        )
    records.extend(
        _between(PymePedido.query.filter(legacy_scope), PymePedido.fecha, start_date, end_date)
        .order_by(PymePedido.fecha.desc())
        .all()
    )
    records.extend(
        _between(MarketOrder.legacy_safe_query().filter_by(tenant_id=tenant.id), MarketOrder.created_at, start_date, end_date)
        .order_by(MarketOrder.created_at.desc())
        .all()
    )
    records.extend(
        _between(PedidoConversacional.query.filter_by(tenant_id=tenant.id), PedidoConversacional.created_at, start_date, end_date)
        .order_by(PedidoConversacional.created_at.desc())
        .all()
    )
    records.extend(
        _between(Order.query.filter_by(tenant_id=tenant.id), Order.created_at, start_date, end_date)
        .order_by(Order.created_at.desc())
        .all()
    )
    safe_records = [_commerce_safe_record(record) for record in records]
    deduped = dedupe_unified_orders(safe_records)
    conversational_by_id = {
        str(item.get("source_id")): item
        for item in safe_records
        if item.get("source_model") == "PedidoConversacional"
    }
    for item in deduped:
        if item.get("source_model") != "MarketOrder":
            continue
        metadata = _as_dict(item.get("metadata"))
        external_refs = _as_dict(item.get("external_refs"))
        linked_id = metadata.get("source_conversational_id")
        if not linked_id and external_refs.get("provider") == "pedido_conversacional":
            linked_id = external_refs.get("order_id")
        linked = conversational_by_id.get(str(linked_id or ""))
        if not linked:
            continue
        item["assisted"] = bool(linked.get("assisted"))
        item["needs_review"] = bool(linked.get("needs_review"))
        item["review_summary"] = _as_dict(linked.get("review_summary"))
        item["request_kind"] = _commerce_request_kind(linked.get("request_kind") or item.get("request_kind"))
        if _as_dict(item.get("location")).get("lat") is None:
            item["location"] = _as_dict(linked.get("location"))
    deduped.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return deduped, len(safe_records)


def _commerce_metrics(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    commerce_records: list[dict[str, Any]] | None = None,
    raw_record_count: int | None = None,
) -> dict[str, Any]:
    if commerce_records is None:
        commerce_records, raw_record_count = _collect_commerce_records(tenant, start_date, end_date)
    orders = commerce_records
    raw_count = int(raw_record_count if raw_record_count is not None else len(orders))

    by_state: Counter[str] = Counter()
    by_origin: Counter[str] = Counter()
    by_source_model: Counter[str] = Counter()
    by_request_kind: Counter[str] = Counter()
    currency_counts: Counter[str] = Counter()
    amounts_by_currency: defaultdict[str, float] = defaultdict(float)
    review_items: list[dict[str, Any]] = []
    assisted_orders = 0
    orders_needing_review = 0
    detected_items = 0
    matched_items = 0
    unmatched_items = 0

    for order in orders:
        metadata = _as_dict(order.get("metadata"))
        needs_review = bool(order.get("needs_review"))
        summary = _as_dict(order.get("review_summary"))
        source = _json_object(metadata.get("source"))
        request_kind = _commerce_request_kind(
            _clean_text(order.get("request_kind"))
            or _clean_text(metadata.get("request_kind"))
            or _clean_text(source.get("request_kind"))
        )
        origin = _norm(order.get("channel"), "unknown")
        state = _norm(order.get("status"), "unknown")
        source_model = str(order.get("source_model") or "Unknown")
        currency = _commerce_currency(order.get("currency"))
        amount = _float_or_none(order.get("total")) or 0.0
        detected = int(summary.get("detected") or 0)
        matched = int(summary.get("matched") or 0)
        unmatched = int(summary.get("unmatched") or 0)

        by_state[state] += 1
        by_origin[origin] += 1
        by_source_model[source_model] += 1
        currency_counts[currency] += 1
        amounts_by_currency[currency] += amount
        by_request_kind[_norm(request_kind, "pedido")] += 1
        detected_items += detected
        matched_items += matched
        unmatched_items += unmatched

        is_assisted = bool(order.get("assisted"))
        if is_assisted:
            assisted_orders += 1
        if not needs_review:
            continue

        orders_needing_review += 1
        priority = "high" if unmatched >= 2 or detected == 0 else "medium"
        record_id = order.get("source_id")
        admin_order_id = str(order.get("id") or f"conversational:{record_id}")
        review_items.append(
            {
                "id": f"assisted_order:{record_id}",
                "record_id": record_id,
                "title": "Pedido asistido requiere revision",
                "label": f"Pedido asistido #{record_id}",
                "priority": priority,
                "reason_code": "assisted_order_review",
                "state": state,
                "origin": origin,
                "request_kind": request_kind,
                "detected": detected,
                "matched": matched,
                "unmatched": unmatched,
                "channel": origin,
                "endpoint": f"/api/admin/tenants/{tenant.slug}/orders/{quote(admin_order_id, safe=':')}",
                "frontend_path": f"/perfil?tab=pedidos&order_id={quote(admin_order_id, safe='')}&focus=assisted_order_queue",
                "ui_hint": "open_assisted_order_review",
                "pii": {"redacted": True},
            }
        )

    review_items.sort(key=lambda item: {"high": 0, "medium": 1, "low": 2}.get(_norm(item.get("priority"), "low"), 9))
    totals_by_currency = [
        {
            "key": currency,
            "label": currency,
            "currency": currency,
            "amount": round(amount, 2),
            "count": int(currency_counts.get(currency) or 0),
        }
        for currency, amount in sorted(amounts_by_currency.items())
    ]
    single_currency = totals_by_currency[0] if len(totals_by_currency) == 1 else None
    return {
        "contract_version": "operations.commerce.v1",
        "summary": {
            "orders": len(orders),
            "source_records": raw_count,
            "deduplicated_mirrors": max(0, raw_count - len(orders)),
            "assisted_orders": assisted_orders,
            "orders_needing_review": orders_needing_review,
            "detected_items": detected_items,
            "matched_items": matched_items,
            "unmatched_items": unmatched_items,
            "review_rate": round((orders_needing_review / len(orders)) * 100, 2) if orders else 0.0,
            "total_monetary": single_currency.get("amount") if single_currency else None,
            "currency": single_currency.get("currency") if single_currency else None,
            "currencies": len(totals_by_currency),
        },
        "by_state": _counter(by_state),
        "by_origin": _counter(by_origin),
        "by_source_model": _counter(by_source_model),
        "by_request_kind": _counter(by_request_kind),
        "totals_by_currency": totals_by_currency,
        "review_items": review_items[:8],
        "frontend_contract": {
            "render_as": "commerce_assisted_ops",
            "recommended_widgets": ["orders_pipeline", "assisted_order_queue", "source_mix", "operator_review_rate", "commerce_heatmap"],
            "empty_state_behavior": "show_upload_or_whatsapp_intake_cta",
            "safe_for_public_demo": True,
            "pii_policy": "redacted",
        },
    }


def _employee_metrics(tenant: TenantProfile, records: list[dict[str, Any]]) -> dict[str, Any]:
    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).order_by(User.id.asc()).all()
    open_records = [record for record in records if record["status"] not in _CLOSED_STATES]
    workload = Counter(str(record.get("assignee_id")) for record in open_records if record.get("assignee_id"))
    categories = sorted({record["category"] for record in open_records})
    channels = sorted({record["channel"] for record in open_records})

    items = []
    covered_categories = set()
    covered_channels = set()
    for employee in employees:
        scope = _as_dict((_as_dict(employee.accesibilidad)).get("employee_scope"))
        emp_categories = {str(item).strip().lower() for item in scope.get("categorias", []) if str(item).strip()}
        emp_channels = {str(item).strip().lower() for item in scope.get("channels", []) if str(item).strip()}
        covered_categories.update(emp_categories)
        covered_channels.update(emp_channels)
        items.append(
            {
                "id": employee.id,
                "name": employee.name,
                "email": employee.email,
                "role": employee.rol,
                "scope": {
                    "categorias": sorted(emp_categories),
                    "channels": sorted(emp_channels),
                    "zonas": [str(item).strip().lower() for item in scope.get("zonas", []) if str(item).strip()],
                },
                "open_workload": workload.get(str(employee.id), 0),
            }
        )

    total_dimensions = len(categories) + len(channels)
    covered_dimensions = len(set(categories) & covered_categories) + len(set(channels) & covered_channels)
    coverage_rate = round((covered_dimensions / total_dimensions) * 100, 2) if total_dimensions else 100.0

    return {
        "summary": {
            "employees": len(employees),
            "assigned_open_tickets": sum(workload.values()),
            "coverage_rate": coverage_rate,
            "unassigned_open_tickets": len([record for record in open_records if not record.get("assignee_id")]),
        },
        "items": items,
        "coverage": {
            "categories": categories,
            "channels": channels,
            "uncovered_categories": sorted(set(categories) - covered_categories),
            "uncovered_channels": sorted(set(channels) - covered_channels),
        },
    }


def _active_presence(records: list[dict[str, Any]]) -> dict[str, Any]:
    ids_by_type: dict[str, set[int]] = defaultdict(set)
    for record in records:
        ids_by_type[record["source"]].add(int(record["id"]))

    states = TicketRealtimeState.query.filter(TicketRealtimeState.presence_status.in_(_ACTIVE_PRESENCE)).all()
    active = []
    for state in states:
        ticket_type = _norm(state.ticket_type, "")
        ticket_id = int(state.ticket_id or 0)
        matches = (
            (ticket_type in {"tenant", "tenant_ticket"} and ticket_id in ids_by_type["tenant_ticket"])
            or (ticket_type in {"municipio", "municipio_ticket"} and ticket_id in ids_by_type["municipio_ticket"])
            or (ticket_type in {"pyme", "pyme_ticket"} and ticket_id in ids_by_type["pyme_ticket"])
        )
        if matches:
            active.append(state)

    by_role = Counter(_norm(state.viewer_role, "unknown") for state in active)
    return {
        "active_viewers": len(active),
        "by_role": _counter(by_role),
        "items": [
            {
                "ticket_type": state.ticket_type,
                "ticket_id": state.ticket_id,
                "viewer_role": state.viewer_role,
                "presence_status": state.presence_status,
                "last_presence_at": _iso(state.last_presence_at),
            }
            for state in active[:30]
        ],
    }


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_datetime(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_datetime(parsed)


def _latest_from_query(query, column) -> datetime | None:
    row = query.order_by(column.desc()).first()
    if not row:
        return None
    return _aware_datetime(getattr(row, column.key, None))


def _max_datetime(*values: Any) -> datetime | None:
    normalized = [_aware_datetime(value) for value in values]
    normalized = [value for value in normalized if value is not None]
    if not normalized:
        return None
    return max(normalized)


def _age_seconds(value: datetime | None) -> int | None:
    if not value:
        return None
    return max(0, int((datetime.now(timezone.utc) - value).total_seconds()))


def _freshness_item(
    *,
    key: str,
    label: str,
    latest_at: datetime | None,
    period_count: int,
    stale_after_seconds: int,
    empty_reason: str,
    recommended_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    age = _age_seconds(latest_at)
    if latest_at is None and period_count == 0:
        status = "empty"
        reason_code = empty_reason
    elif age is not None and age > stale_after_seconds:
        status = "stale"
        reason_code = "source_stale"
    else:
        status = "fresh"
        reason_code = "source_fresh"

    return {
        "key": key,
        "label": label,
        "status": status,
        "reason_code": reason_code,
        "period_count": int(period_count or 0),
        "latest_at": _iso(latest_at),
        "age_seconds": age,
        "stale_after_seconds": stale_after_seconds,
        "recommended_action": recommended_action or {},
    }


def build_operational_freshness(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
) -> dict[str, Any]:
    ticket_records = _collect_ticket_records(tenant, start_date, end_date, viewer=viewer)
    tenant_ticket_query = apply_employee_ticket_category_scope(
        TenantTicket.query.filter_by(tenant_id=tenant.id),
        viewer,
        TenantTicket,
    )
    municipio_ticket_query = apply_employee_ticket_category_scope(
        _municipio_ticket_query(tenant),
        viewer,
        MunicipioTicket,
    )
    pyme_ticket_query = apply_employee_ticket_category_scope(
        PymeTicket.query.filter_by(tenant_id=tenant.id),
        viewer,
        PymeTicket,
    )
    ticket_latest = _max_datetime(
        _latest_from_query(tenant_ticket_query, TenantTicket.created_at),
        _latest_from_query(municipio_ticket_query, MunicipioTicket.fecha),
        _latest_from_query(pyme_ticket_query, PymeTicket.fecha),
    )

    if is_employee_heatmap_viewer(viewer):
        heatmap = build_operational_heatmap(
            tenant,
            start_date,
            end_date,
            ticket_records=ticket_records,
            max_points=250,
            include_ai=False,
            commerce_records=[],
            viewer=viewer,
        )
        heatmap_latest = None
        for point in heatmap.get("points") or []:
            timestamp = point.get("timestamp")
            if not timestamp:
                continue
            try:
                parsed = datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            heatmap_latest = _max_datetime(heatmap_latest, parsed)

        ticket_source = _freshness_item(
            key="tickets",
            label="Tickets y reclamos",
            latest_at=ticket_latest,
            period_count=len(ticket_records),
            stale_after_seconds=6 * 60 * 60,
            empty_reason="no_tickets_in_period",
            recommended_action={
                "endpoint": "/api/v2/tickets",
                "ui_hint": "open_ticket_board",
            },
        )
        heatmap_source = _freshness_item(
            key="heatmap",
            label="Mapa operativo",
            latest_at=heatmap_latest,
            period_count=(heatmap.get("summary") or {}).get("points", 0),
            stale_after_seconds=24 * 60 * 60,
            empty_reason="no_geo_points_in_period",
            recommended_action={
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "ui_hint": "open_heatmap",
            },
        )
        unavailable_sources = [
            {
                "key": key,
                "label": label,
                "status": "unavailable",
                "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
                "period_count": None,
                "latest_at": None,
                "age_seconds": None,
                "stale_after_seconds": None,
                "recommended_action": {},
            }
            for key, label in (
                ("surveys", "Encuestas y votaciones"),
                ("analytics_events", "Eventos analytics"),
                ("chats", "Sesiones de chat"),
                ("commerce", "Pedidos, ventas y marketplace"),
            )
        ]
        sources = [ticket_source, heatmap_source, *unavailable_sources]
        has_operational_data = bool(
            len(ticket_records)
            or int((heatmap.get("summary") or {}).get("points") or 0)
        )
        if not has_operational_data:
            status = "empty"
            reason_code = "no_scoped_operational_data_in_period"
        elif ticket_source["status"] == "stale" or heatmap_source["status"] == "stale":
            status = "degraded"
            reason_code = "one_or_more_scoped_sources_stale"
        else:
            status = "fresh"
            reason_code = "all_scoped_sources_fresh"

        latest_at = _max_datetime(ticket_latest, heatmap_latest)
        return {
            "contract_version": "operations.freshness.v1",
            "tenant": _tenant_ref(tenant),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {"from": _iso(start_date), "to": _iso(end_date)},
            "scope": _operations_scope_contract(employee_view=True),
            "status": status,
            "reason_code": reason_code,
            "summary": {
                "sources": len(sources),
                "scoped_sources": 2,
                "unavailable_sources": len(unavailable_sources),
                "fresh_sources": len(
                    [item for item in (ticket_source, heatmap_source) if item["status"] == "fresh"]
                ),
                "stale_sources": len(
                    [item for item in (ticket_source, heatmap_source) if item["status"] == "stale"]
                ),
                "empty_sources": len(
                    [item for item in (ticket_source, heatmap_source) if item["status"] == "empty"]
                ),
                "latest_at": _iso(latest_at),
                "employee_count": None,
                "has_operational_data": has_operational_data,
                "can_render_dashboard": has_operational_data,
                "can_render_heatmap": bool(
                    (heatmap.get("summary") or {}).get("points", 0)
                ),
            },
            "sources": sources,
            "response_provenance": {
                "available": False,
                "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
            },
            "frontend_contract": {
                "render_as": "analytics_freshness",
                "primary_refresh_seconds": 60,
                "empty_state_behavior": "show_reason_code",
                "degraded_state_behavior": "show_stale_sources",
                "unavailable_state_behavior": "show_scope_restriction",
            },
        }

    period_response_condition = and_(
        EncRespuesta.submitted_at >= start_date,
        EncRespuesta.submitted_at <= end_date,
    )
    enc_response_stats = {
        str(origin or ""): {
            "latest_at": latest_at,
            "period_count": int(period_count or 0),
        }
        for origin, latest_at, period_count in (
            EncRespuesta.query.filter_by(tenant_id=tenant.id)
            .with_entities(
                EncRespuesta.response_origin,
                func.max(EncRespuesta.submitted_at),
                func.sum(case((period_response_condition, 1), else_=0)),
            )
            .group_by(EncRespuesta.response_origin)
            .all()
        )
    }
    real_response_stats = enc_response_stats.get(
        SURVEY_RESPONSE_ORIGIN_REAL,
        {"latest_at": None, "period_count": 0},
    )
    synthetic_response_stats = enc_response_stats.get(
        SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
        {"latest_at": None, "period_count": 0},
    )
    unverified_response_stats = enc_response_stats.get(
        SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
        {"latest_at": None, "period_count": 0},
    )
    enc_response_latest = real_response_stats["latest_at"]
    survey_response_count = int(real_response_stats["period_count"] or 0)
    period_synthetic_response_count = int(
        synthetic_response_stats["period_count"] or 0
    )
    period_unverified_response_count = int(
        unverified_response_stats["period_count"] or 0
    )

    public_period_condition = and_(
        PublicSurveyResponse.created_at >= start_date,
        PublicSurveyResponse.created_at <= end_date,
    )
    public_response_latest, public_response_count = (
        PublicSurveyResponse.query.join(
            PublicSurvey,
            PublicSurveyResponse.survey_id == PublicSurvey.id,
        )
        .filter(PublicSurvey.tenant_id == tenant.id)
        .with_entities(
            func.max(PublicSurveyResponse.created_at),
            func.sum(case((public_period_condition, 1), else_=0)),
        )
        .first()
    )
    public_response_count = int(public_response_count or 0)
    survey_latest = _max_datetime(enc_response_latest, public_response_latest)

    event_query = AnalyticsEventV2.query.filter_by(tenant_id=tenant.id)
    analytics_event_count = _between(event_query, AnalyticsEventV2.ts, start_date, end_date).count()
    analytics_latest = _latest_from_query(event_query, AnalyticsEventV2.ts)

    chat_query = ChatSessionContext.query.filter_by(tenant_id=tenant.id)
    chat_count = _between(chat_query, ChatSessionContext.last_updated, start_date, end_date).count()
    chat_latest = _latest_from_query(chat_query, ChatSessionContext.last_updated)

    commerce_records, _ = _collect_commerce_records(tenant, start_date, end_date)
    order_count = len(commerce_records)
    order_latest = _max_datetime(
        *(
            _parse_datetime(order.get("updated_at") or order.get("created_at"))
            for order in commerce_records
        )
    )

    employee_count = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).count()
    heatmap = build_operational_heatmap(
        tenant,
        start_date,
        end_date,
        ticket_records=ticket_records,
        max_points=250,
        include_ai=False,
        commerce_records=commerce_records,
        viewer=viewer,
    )
    heatmap_latest = None
    for point in heatmap.get("points") or []:
        timestamp = point.get("timestamp")
        if not timestamp:
            continue
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        heatmap_latest = _max_datetime(heatmap_latest, parsed)

    sources = [
        _freshness_item(
            key="tickets",
            label="Tickets y reclamos",
            latest_at=ticket_latest,
            period_count=len(ticket_records),
            stale_after_seconds=6 * 60 * 60,
            empty_reason="no_tickets_in_period",
            recommended_action={"endpoint": "/api/v2/tickets", "ui_hint": "open_ticket_board"},
        ),
        _freshness_item(
            key="surveys",
            label="Encuestas y votaciones",
            latest_at=survey_latest,
            period_count=survey_response_count + public_response_count,
            stale_after_seconds=24 * 60 * 60,
            empty_reason="no_survey_responses_in_period",
            recommended_action={"endpoint": "/api/v2/surveys", "ui_hint": "open_survey_monitor"},
        ),
        _freshness_item(
            key="analytics_events",
            label="Eventos analytics",
            latest_at=analytics_latest,
            period_count=analytics_event_count,
            stale_after_seconds=2 * 60 * 60,
            empty_reason="no_analytics_events_in_period",
            recommended_action={"endpoint": "/api/v2/analytics/operations/dashboard", "ui_hint": "check_tracking"},
        ),
        _freshness_item(
            key="chats",
            label="Sesiones de chat",
            latest_at=chat_latest,
            period_count=chat_count,
            stale_after_seconds=2 * 60 * 60,
            empty_reason="no_chat_sessions_in_period",
            recommended_action={"endpoint": "/api/v2/inbox/omnichannel", "ui_hint": "open_live_chat"},
        ),
        _freshness_item(
            key="commerce",
            label="Pedidos, ventas y marketplace",
            latest_at=order_latest,
            period_count=order_count,
            stale_after_seconds=6 * 60 * 60,
            empty_reason="no_orders_in_period",
            recommended_action={"endpoint": f"/api/admin/tenants/{tenant.slug}/orders", "ui_hint": "open_orders_pipeline"},
        ),
        _freshness_item(
            key="heatmap",
            label="Mapa operativo",
            latest_at=heatmap_latest,
            period_count=(heatmap.get("summary") or {}).get("points", 0),
            stale_after_seconds=24 * 60 * 60,
            empty_reason="no_geo_points_in_period",
            recommended_action={"endpoint": "/api/v2/analytics/operations/heatmap", "ui_hint": "open_heatmap"},
        ),
    ]

    stale_sources = [item for item in sources if item["status"] == "stale"]
    empty_sources = [item for item in sources if item["status"] == "empty"]
    latest_at = _max_datetime(*(datetime.fromisoformat(item["latest_at"]) for item in sources if item.get("latest_at")))
    has_operational_data = any(item["period_count"] > 0 for item in sources)

    if not has_operational_data:
        status = "empty"
        reason_code = "no_operational_data_in_period"
    elif stale_sources:
        status = "degraded"
        reason_code = "one_or_more_sources_stale"
    else:
        status = "fresh"
        reason_code = "all_sources_fresh"

    return {
        "contract_version": "operations.freshness.v1",
        "tenant": _tenant_ref(tenant),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "status": status,
        "reason_code": reason_code,
        "summary": {
            "sources": len(sources),
            "fresh_sources": len([item for item in sources if item["status"] == "fresh"]),
            "stale_sources": len(stale_sources),
            "empty_sources": len(empty_sources),
            "latest_at": _iso(latest_at),
            "employee_count": employee_count,
            "has_operational_data": has_operational_data,
            "can_render_dashboard": has_operational_data,
            "can_render_heatmap": bool((heatmap.get("summary") or {}).get("points", 0)),
        },
        "sources": sources,
        "response_provenance": build_survey_response_provenance(
            real_count=survey_response_count + public_response_count,
            synthetic_count=period_synthetic_response_count,
            unverified_count=period_unverified_response_count,
            mode="real",
        ),
        "frontend_contract": {
            "render_as": "analytics_freshness",
            "primary_refresh_seconds": 60,
            "empty_state_behavior": "show_reason_code",
            "degraded_state_behavior": "show_stale_sources",
        },
    }


def _record_to_filter_probe(record: dict[str, Any]) -> dict[str, Any]:
    demographics = _as_dict(record.get("demographics"))
    return {
        "source": "ticket",
        "record_source": record.get("source"),
        "category": record.get("category"),
        "channel": record.get("channel"),
        "status": record.get("status"),
        "zone": record.get("zone"),
        "sla_state": record.get("sla_state"),
        "assignee_id": str(record.get("assignee_id")) if record.get("assignee_id") is not None else None,
        "gender": demographics.get("gender") or "unknown",
        "age_range": demographics.get("age_range") or "unknown",
        "address": record.get("address"),
    }


def _crm_tickets_href(
    *,
    focus: str | None = None,
    source_model: Any | None = None,
    ticket_id: Any | None = None,
    category: Any | None = None,
    channel: Any | None = None,
    status: Any | None = None,
    heatmap_cell: Any | None = None,
    sla: Any | None = None,
    assignee: Any | None = None,
) -> str:
    params: list[tuple[str, Any]] = [("tab", "tickets")]
    if source_model is not None:
        params.append(("source_model", source_model))
    if ticket_id is not None:
        params.append(("ticket_id", ticket_id))
    if focus:
        params.append(("focus", focus))
    if category:
        params.append(("categoria", category))
    if channel:
        params.append(("canal", channel))
    if status:
        params.append(("estado", status))
    if heatmap_cell:
        params.append(("heatmap_cell", heatmap_cell))
    if sla:
        params.append(("sla", sla))
    if assignee is not None:
        params.append(("agent", assignee))
    return "/perfil?" + "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params)


def _opaque_ticket_id(value: Any) -> str | None:
    """Return a lossless ticket identifier suitable for URL serialization.

    Ticket identity is an opaque pair.  In particular, string identifiers must
    never be parsed as integers because doing so would discard leading zeroes
    and could open a different record.  Booleans and non-integral numeric
    values are rejected rather than coerced into a plausible identifier.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value or value != value.strip():
            return None
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _ticket_action_identity(record: dict[str, Any]) -> tuple[str, str, Any, str] | None:
    record_source = record.get("source")
    if not isinstance(record_source, str):
        return None
    expected_source_model = _TICKET_SOURCE_MODEL_BY_RECORD_SOURCE.get(record_source)
    source_model = record.get("source_model")
    if not expected_source_model or source_model != expected_source_model:
        return None
    record_id = record.get("id")
    opaque_ticket_id = _opaque_ticket_id(record_id)
    if opaque_ticket_id is None:
        return None
    return record_source, source_model, record_id, opaque_ticket_id


def _ticket_action_contract(record: dict[str, Any]) -> list[dict[str, Any]]:
    identity = _ticket_action_identity(record)
    if identity is None:
        return []
    record_source, source_model, record_id, opaque_ticket_id = identity
    encoded_ticket_id = quote(opaque_ticket_id, safe="")

    source_config = {
        "tenant_ticket": {
            "open_endpoint": f"/api/v2/tickets/{encoded_ticket_id}",
            "location_endpoint": f"/api/v2/tickets/{encoded_ticket_id}",
            "location_method": "PATCH",
            "location_body_template": {"location": {"lat": "number", "lng": "number", "address": "string"}},
            "requires": ["location.lat", "location.lng"],
        },
        "municipio_ticket": {
            "open_endpoint": f"/tickets/municipio/{encoded_ticket_id}",
            "location_endpoint": f"/tickets/municipio/{encoded_ticket_id}/ubicacion",
            "location_method": "PUT",
            "location_body_template": {"latitud": "number", "longitud": "number", "direccion": "string"},
            "requires": ["latitud", "longitud"],
        },
        "pyme_ticket": {
            "open_endpoint": f"/tickets/pyme/{encoded_ticket_id}",
            "location_endpoint": f"/tickets/pyme/{encoded_ticket_id}/ubicacion",
            "location_method": "PUT",
            "location_body_template": {"latitud": "number", "longitud": "number", "direccion": "string"},
            "requires": ["latitud", "longitud"],
        },
    }.get(record_source)

    if not source_config:
        return []

    open_href = _crm_tickets_href(
        source_model=source_model,
        ticket_id=opaque_ticket_id,
        focus="open_ticket_detail",
        category=record.get("category"),
        channel=record.get("channel"),
        status=record.get("status"),
    )
    geocoding_href = _crm_tickets_href(
        source_model=source_model,
        ticket_id=opaque_ticket_id,
        focus="open_geocoding_queue",
        category=record.get("category"),
        channel=record.get("channel"),
        status=record.get("status"),
        sla="risk",
    )

    return [
        {
            "id": "open_record",
            "label": "Abrir caso",
            "type": "api",
            "method": "GET",
            "endpoint": source_config["open_endpoint"],
            "href": open_href,
            "frontend_path": open_href,
            "action_type": "open_record",
            "writes_enabled": False,
            "record_source": record_source,
            "record_id": record_id,
            "source_model": source_model,
            "ticket_id": record_id,
        },
        {
            "id": "update_location",
            "label": "Actualizar ubicacion",
            "type": "api",
            "method": source_config["location_method"],
            "endpoint": source_config["location_endpoint"],
            "href": geocoding_href,
            "frontend_path": geocoding_href,
            "action_type": "update_location",
            "writes_enabled": True,
            "record_source": record_source,
            "record_id": record_id,
            "source_model": source_model,
            "ticket_id": record_id,
            "requires": source_config["requires"],
            "body_template": source_config["location_body_template"],
        },
    ]


def _geocoding_candidate(record: dict[str, Any]) -> dict[str, Any]:
    demographics = _as_dict(record.get("demographics"))
    return {
        "id": f"{record.get('source')}:{record.get('id')}",
        "record_source": record.get("source"),
        "record_id": record.get("id"),
        "source_model": record.get("source_model"),
        "ticket_id": record.get("id"),
        "category": record.get("category"),
        "raw_category": record.get("raw_category"),
        "category_provenance": record.get("category_provenance"),
        "channel": record.get("channel"),
        "status": record.get("status"),
        "zone": record.get("zone"),
        "address": record.get("address"),
        "label": record.get("title"),
        "timestamp": _iso(record.get("created_at")),
        "gender": demographics.get("gender") or "unknown",
        "age_range": demographics.get("age_range") or "unknown",
        "reason_code": "address_without_coordinates",
        "sla_state": record.get("sla_state") or "normal",
        "overdue": bool(record.get("overdue")),
        "assignee_id": record.get("assignee_id"),
        "location_provenance": record.get("location_provenance"),
        "actions": _ticket_action_contract(record),
    }


def _reported_location_review_candidate(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{record.get('source')}:{record.get('id')}",
        "record_source": record.get("source"),
        "record_id": record.get("id"),
        "source_model": record.get("source_model"),
        "ticket_id": record.get("id"),
        "category": record.get("category"),
        "raw_category": record.get("raw_category"),
        "category_provenance": record.get("category_provenance"),
        "reported_location_text": record.get("reported_location_text"),
        "reason_code": "reported_location_text_requires_review",
        "location_provenance": record.get("location_provenance"),
        "automatic_geocoding": False,
        "writes_performed": False,
    }


def _location_provenance_source(record: dict[str, Any], field: str) -> str:
    field_evidence = _as_dict(_as_dict(record.get("location_provenance")).get(field))
    return _norm(field_evidence.get("source"), "missing")


def _location_quality(
    records: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    *,
    require_verified_jurisdiction: bool = False,
    jurisdiction_verified: bool = False,
) -> dict[str, Any]:
    ticket_records = [record for record in records if record.get("source") in {"tenant_ticket", "municipio_ticket", "pyme_ticket"}]
    with_persisted_coordinates = [record for record in ticket_records if _record_has_coordinates(record)]
    coordinate_dispositions = {
        id(record): coordinate_jurisdiction_disposition(
            record.get("coordinate_jurisdiction_status"),
            require_verified_jurisdiction=require_verified_jurisdiction,
            jurisdiction_verified=jurisdiction_verified,
        )
        for record in with_persisted_coordinates
    }
    outside_jurisdiction = [
        record
        for record in with_persisted_coordinates
        if coordinate_dispositions[id(record)] == "outside"
    ]
    unverified_jurisdiction = [
        record
        for record in with_persisted_coordinates
        if coordinate_dispositions[id(record)] == "unverified"
    ]
    with_coordinates = [
        record
        for record in with_persisted_coordinates
        if _jurisdiction_status_allows_map(
            str(record.get("coordinate_jurisdiction_status") or "missing"),
            require_verified_jurisdiction=require_verified_jurisdiction,
            jurisdiction_verified=jurisdiction_verified,
        )
    ]
    with_address = [record for record in ticket_records if record.get("address")]
    with_zone = [record for record in ticket_records if explicit_zone(record.get("zone"))]
    with_address_and_coordinates = [
        record
        for record in ticket_records
        if record.get("address")
        and _record_has_coordinates(record)
        and _jurisdiction_status_allows_map(
            str(record.get("coordinate_jurisdiction_status") or "missing"),
            require_verified_jurisdiction=require_verified_jurisdiction,
            jurisdiction_verified=jurisdiction_verified,
        )
    ]
    missing_location = [
        record
        for record in ticket_records
        if not _record_has_coordinates(record) and not record.get("address")
    ]
    total = len(ticket_records)
    coverage_pct = round((len(with_coordinates) / total) * 100, 2) if total else 0.0
    coordinate_sources = Counter(
        _location_provenance_source(record, "coordinate") for record in with_coordinates
    )
    address_sources = Counter(
        _location_provenance_source(record, "address") for record in with_address
    )
    zone_sources = Counter(
        _location_provenance_source(record, "zone") for record in with_zone
    )
    return {
        "contract_version": "operations.location_quality.v1",
        "total_ticket_records": total,
        "ticket_records_with_persisted_coordinates": len(with_persisted_coordinates),
        "ticket_records_with_coordinates": len(with_coordinates),
        "ticket_records_with_validated_coordinates": len(with_coordinates),
        "ticket_records_outside_jurisdiction": len(outside_jurisdiction),
        "ticket_records_unverified_jurisdiction": len(unverified_jurisdiction),
        "ticket_records_with_address": len(with_address),
        "ticket_records_with_address_and_coordinates": len(with_address_and_coordinates),
        "ticket_records_with_explicit_zone": len(with_zone),
        "ticket_records_pending_geocode": len(geocoding_candidates),
        "ticket_records_without_location": len(missing_location),
        "ticket_records_recovered_from_metadata": len(
            [
                record
                for record in with_coordinates
                if _location_provenance_source(record, "coordinate") == "ticket_metadata"
            ]
        ),
        "coordinate_coverage_pct": coverage_pct,
        "provenance": {
            "coordinate_sources": _counter(coordinate_sources),
            "address_sources": _counter(address_sources),
            "zone_sources": _counter(zone_sources),
            "external_geocoding_calls": 0,
            "writes_performed": False,
        },
        "status": (
            "ready"
            if with_coordinates
            else (
                "jurisdiction_unverified"
                if unverified_jurisdiction
                else ("pending_geocode" if geocoding_candidates else "empty")
            )
        ),
        "reason_code": (
            "coordinates_available"
            if with_coordinates
            else (
                "tenant_jurisdiction_unverified"
                if unverified_jurisdiction
                else (
                    "coordinates_outside_configured_jurisdiction"
                    if outside_jurisdiction
                    else ("addresses_need_geocoding" if geocoding_candidates else "no_ticket_locations")
                )
            )
        ),
    }


def _heatmap_quality_contract(
    *,
    points: list[dict[str, Any]],
    records: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    location_quality: dict[str, Any],
    max_points: int,
    jurisdiction_blocked: bool = False,
) -> dict[str, Any]:
    total_ticket_records = int(location_quality.get("total_ticket_records") or 0)
    ticket_records_with_coordinates = int(location_quality.get("ticket_records_with_coordinates") or 0)
    pending_geocode = len(geocoding_candidates or [])
    visible_points = len(points or [])
    ticket_coverage_rate = round(ticket_records_with_coordinates / total_ticket_records, 4) if total_ticket_records else 0.0
    has_survey_or_event_points = any(
        point.get("source") in {"survey", "analytics_event", "commerce"}
        for point in points or []
    )

    if visible_points:
        if total_ticket_records and ticket_coverage_rate < 0.35 and not has_survey_or_event_points:
            state = "partial"
            reason_code = "low_ticket_coordinate_coverage"
            label = "Cobertura territorial parcial"
        else:
            state = "ready"
            reason_code = "ready"
            label = "Mapa operativo confiable"
    elif jurisdiction_blocked:
        state = "blocked"
        reason_code = "official_jurisdiction_boundary_unavailable"
        label = "Alcance territorial oficial no disponible"
    elif pending_geocode:
        state = "pending_geocode"
        reason_code = "addresses_need_geocoding"
        label = "Direcciones pendientes de geocodificar"
    elif records:
        state = "blocked"
        reason_code = "missing_coordinates"
        label = "Sin coordenadas reales"
    else:
        state = "empty"
        reason_code = "no_operational_events"
        label = "Sin eventos para el periodo"

    return {
        "contract_version": "operations.heatmap_quality.v1",
        "state": state,
        "label": label,
        "reason_code": reason_code,
        "coverage_rate": ticket_coverage_rate,
        "coverage_percent": round(ticket_coverage_rate * 100, 1),
        "visible_points": visible_points,
        "total_ticket_records": total_ticket_records,
        "ticket_records_with_coordinates": ticket_records_with_coordinates,
        "ticket_records_without_coordinates": max(0, total_ticket_records - ticket_records_with_coordinates),
        "pending_geocode": pending_geocode,
        "max_points": max_points,
        "can_render_heatmap": bool(visible_points),
        "empty_state_action": {
            "label": "Capturar ubicacion en WhatsApp",
            "description": "Pedir ubicacion o geocodificar direcciones mejora el mapa, los hotspots y la asignacion por zona.",
            "endpoint": "/api/v2/analytics/operations/heatmap",
            "ui_hint": "open_geocoding_queue",
        },
    }


def _heatmap_realtime_contract(points: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps: list[datetime] = []
    for point in points or []:
        parsed = _parse_datetime(point.get("timestamp"))
        if parsed:
            timestamps.append(parsed)

    latest = max(timestamps) if timestamps else None
    return {
        "contract_version": "operations.heatmap_realtime.v1",
        "poll_seconds": 20,
        "socket_namespace": "analytics",
        "socket_events": ["ticket.updated", "survey.vote.created", "whatsapp.message.created", "analytics.event.created"],
        "latest_event_at": _iso(latest) if latest else None,
        "sources": ["tickets", "surveys", "analytics_events", "whatsapp"],
    }


def _heatmap_operational_rank_reason(signals: dict[str, int]) -> str:
    if int(signals.get("breached_sla") or 0) > 0:
        return "sla_breached"
    if int(signals.get("overdue") or 0) > 0:
        return "overdue_cases"
    if int(signals.get("unassigned") or 0) > 0:
        return "unassigned_cases"
    if int(signals.get("recent_24h") or 0) > 0:
        return "recent_activity"
    if int(signals.get("tickets") or 0) > 0:
        return "ticket_density"
    return "activity_density"


def _heatmap_operational_score(cell: dict[str, Any]) -> float:
    breached = int(cell.get("breached_sla_count") or 0)
    overdue = int(cell.get("overdue_count") or 0)
    unassigned = int(cell.get("unassigned_count") or 0)
    recent = int(cell.get("recent_24h_count") or 0)
    tickets = int(cell.get("ticket_count") or 0)
    surveys = int(cell.get("survey_count") or 0)
    events = int(cell.get("event_count") or 0)
    score = (
        float(cell.get("weight") or 0) * 1.5
        + int(cell.get("count") or 0)
        + breached * 8
        + overdue * 6
        + unassigned * 4
        + recent * 3
        + min(tickets, 10) * 1.5
        + min(surveys, 10) * 0.6
        + min(events, 10) * 0.4
    )
    return round(score, 2)


def _heatmap_operational_hotspots(cell_items: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    hotspots: list[dict[str, Any]] = []
    for cell in cell_items:
        signals = {
            "overdue": int(cell.get("overdue_count") or 0),
            "unassigned": int(cell.get("unassigned_count") or 0),
            "breached_sla": int(cell.get("breached_sla_count") or 0),
            "recent_24h": int(cell.get("recent_24h_count") or 0),
            "tickets": int(cell.get("ticket_count") or 0),
            "surveys": int(cell.get("survey_count") or 0),
            "analytics_events": int(cell.get("event_count") or 0),
        }
        score = _heatmap_operational_score(cell)
        top_category = (cell.get("top_categories") or [{}])[0].get("key")
        top_channel = (cell.get("top_channels") or [{}])[0].get("key")
        hotspot_href = _crm_tickets_href(
            focus="focus_map_cell_and_filter_tickets",
            category=top_category,
            channel=top_channel,
            heatmap_cell=cell.get("id"),
        )
        hotspots.append(
            {
                "id": cell.get("id"),
                "lat": cell.get("lat"),
                "lng": cell.get("lng"),
                "weight": cell.get("weight"),
                "count": cell.get("count"),
                "operational_score": score,
                "rank_reason": _heatmap_operational_rank_reason(signals),
                "latest_event_at": cell.get("latest_event_at"),
                "signals": signals,
                "top_category": top_category,
                "top_channel": top_channel,
                "sources": cell.get("sources") or [],
                "top_categories": cell.get("top_categories") or [],
                "top_channels": cell.get("top_channels") or [],
                "demographics": cell.get("demographics") or {},
                "recommended_action": {
                    "action_id": f"open_operational_hotspot_{cell.get('id')}",
                    "label": "Abrir zona prioritaria",
                    "ui_hint": "focus_map_cell_and_filter_tickets",
                    "href": hotspot_href,
                    "frontend_path": hotspot_href,
                    "filters": {
                        "category": top_category,
                        "channel": top_channel,
                        "cell_id": cell.get("id"),
                    },
                },
            }
        )

    hotspots.sort(
        key=lambda item: (
            float(item.get("operational_score") or 0),
            int((item.get("signals") or {}).get("breached_sla") or 0),
            int((item.get("signals") or {}).get("overdue") or 0),
            int((item.get("signals") or {}).get("recent_24h") or 0),
            int(item.get("count") or 0),
        ),
        reverse=True,
    )
    return hotspots[:limit]


def _bounds_center(bounds: dict[str, Any] | None) -> dict[str, float] | None:
    if not bounds:
        return None
    try:
        return {
            "lat": round((float(bounds["north"]) + float(bounds["south"])) / 2, 6),
            "lng": round((float(bounds["east"]) + float(bounds["west"])) / 2, 6),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _heatmap_narrative_contract(
    tenant: TenantProfile,
    *,
    summary: dict[str, Any],
    quality: dict[str, Any],
    category_layers: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    ai_summary: dict[str, Any],
) -> dict[str, Any]:
    state = str(quality.get("state") or "empty")
    points = int(summary.get("points") or 0)
    cells = int(summary.get("cells") or 0)
    pending_geocode = len(geocoding_candidates or [])
    top_category = (category_layers[0] if category_layers else {}).get("key")
    tenant_type = _norm(getattr(tenant, "tipo", None), "operacion")

    if points:
        headline = f"{points} puntos territoriales listos para decision"
        body = (
            f"El mapa consolida {cells} zonas activas con cobertura "
            f"{quality.get('coverage_percent', 0)}% y senales AI en modo {ai_summary.get('risk_level') or 'normal'}."
        )
    elif quality.get("reason_code") == "official_jurisdiction_boundary_unavailable":
        headline = "Alcance territorial pendiente de validación oficial"
        body = (
            "Hay coordenadas persistidas, pero no se publican puntos, calor ni focos hasta validar "
            "su pertenencia mediante el polígono oficial del tenant."
        )
    elif pending_geocode:
        headline = f"{pending_geocode} direcciones listas para geocodificar"
        body = "La UI puede mostrar la cola territorial y pedir coordenadas antes de pintar calor real."
    elif state == "blocked":
        headline = "Sin coordenadas reales para pintar el territorio"
        body = "Hay actividad operativa, pero falta latitud/longitud o direcciones utiles para construir hotspots."
    else:
        headline = "Sin eventos territoriales para el periodo"
        body = "El mapa queda en estado vacio y puede mostrar configuracion, captura de ubicacion y filtros."

    return {
        "contract_version": "operations.heatmap_narrative.v1",
        "state": state,
        "tenant_type": tenant_type,
        "headline": headline,
        "body": body,
        "primary_metric": {"key": "visible_points", "label": "Puntos visibles", "value": points},
        "secondary_metrics": [
            {"key": "hotspot_cells", "label": "Zonas activas", "value": cells},
            {"key": "coverage_percent", "label": "Cobertura GPS", "value": quality.get("coverage_percent", 0)},
            {"key": "pending_geocode", "label": "Direcciones pendientes", "value": pending_geocode},
        ],
        "focus": {
            "top_category": top_category,
            "risk_level": ai_summary.get("risk_level") or "normal",
            "dominant_intent": ai_summary.get("dominant_intent") or "general_query",
            "requires_human_attention": bool(ai_summary.get("requires_human_attention")),
        },
        "empty_state": {
            "reason_code": quality.get("reason_code"),
            "recommended_view": "geocoding_queue" if pending_geocode else "capture_location_setup",
        },
    }


def _heatmap_viewport_presets(
    *,
    bounds: dict[str, Any] | None,
    hotspots: list[dict[str, Any]],
    quality: dict[str, Any],
    geocoding_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    center = _bounds_center(bounds)
    presets: list[dict[str, Any]] = []
    if bounds and center:
        presets.append(
            {
                "id": "fit_operational_bounds",
                "label": "Territorio activo",
                "mode": "fit_bounds",
                "default": True,
                "bounds": bounds,
                "center": center,
                "padding_px": 72,
                "reason_code": "points_available",
            }
        )
    if hotspots:
        hotspot = hotspots[0]
        presets.append(
            {
                "id": "top_hotspot",
                "label": "Hotspot principal",
                "mode": "fly_to",
                "default": not bool(presets),
                "center": {"lat": hotspot.get("lat"), "lng": hotspot.get("lng")},
                "zoom": 14,
                "bearing": 0,
                "pitch": 45,
                "target_cell_id": hotspot.get("id"),
                "reason_code": "highest_weight_cell",
            }
        )
    if geocoding_candidates:
        presets.append(
            {
                "id": "geocoding_queue",
                "label": "Direcciones pendientes",
                "mode": "queue_panel",
                "default": not bool(presets),
                "reason_code": "addresses_need_geocoding",
                "candidate_count": len(geocoding_candidates),
            }
        )
    if not presets:
        presets.append(
            {
                "id": "tenant_region_empty",
                "label": "Region sin coordenadas",
                "mode": "frontend_default_region",
                "default": True,
                "reason_code": quality.get("reason_code") or "no_points",
                "requires_frontend_default_center": True,
            }
        )

    return {
        "contract_version": "operations.heatmap_viewport_presets.v1",
        "default_preset_id": next((preset["id"] for preset in presets if preset.get("default")), presets[0]["id"]),
        "camera_constraints": {
            "min_zoom": 4,
            "max_zoom": 18,
            "fit_bounds_padding_px": 72,
            "prefer_reduced_motion": False,
        },
        "presets": presets,
    }


def _heatmap_layer_style_contract(
    *,
    ai_layers: dict[str, Any],
    category_layers: list[dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any]:
    style_tokens = _as_dict(ai_layers.get("style_tokens")) or {
        "risk": "#EF4444",
        "whatsapp": "#10B981",
        "survey": "#7C3AED",
        "commerce": "#0EA5E9",
        "attention": "#F59E0B",
        "neutral": "#2563EB",
    }
    return {
        "contract_version": "operations.heatmap_layer_styles.v1",
        "default_engine": "maplibre",
        "compatible_engines": ["maplibre", "deckgl", "google"],
        "quality_state": quality.get("state"),
        "style_tokens": style_tokens,
        "layers": [
            {
                "id": "base_heatmap",
                "source": "points",
                "geometry": "weighted_points",
                "default_visible": True,
                "style": {"type": "heatmap", "weight_field": "weight", "radius_px": 34, "intensity": 0.75},
                "interaction": {"hover": True, "click": "inspect_point"},
            },
            {
                "id": "hotspot_cells",
                "source": "cells",
                "geometry": "cell_centroids",
                "default_visible": True,
                "style": {"type": "circle", "color": style_tokens.get("attention"), "radius_field": "weight"},
                "interaction": {"hover": True, "click": "open_hotspot_actions"},
            },
            {
                "id": "category_layers",
                "source": "category_layers",
                "geometry": "grouped_points",
                "default_visible": bool(category_layers),
                "style": {"type": "category_heatmap", "max_categories": 12},
                "interaction": {"toggle": True, "filter_key": "category"},
            },
            {
                "id": "ai_risk_layers",
                "source": "ai_layers.layers.risk_pulses",
                "geometry": "animated_scatter",
                "default_visible": True,
                "style": {"type": "pulse", "color": style_tokens.get("risk"), "reduced_motion": "static_circle"},
                "interaction": {"click": "inspect_ai_signals"},
            },
            {
                "id": "whatsapp_activity",
                "source": "ai_layers.layers.whatsapp_activity",
                "geometry": "pulse_points",
                "default_visible": True,
                "style": {"type": "pulse", "color": style_tokens.get("whatsapp"), "reduced_motion": "static_circle"},
                "interaction": {"click": "filter_channel_whatsapp"},
            },
            {
                "id": "survey_participation",
                "source": "ai_layers.layers.survey_participation",
                "geometry": "territory_heat",
                "default_visible": True,
                "style": {"type": "heatmap", "color": style_tokens.get("survey")},
                "interaction": {"click": "open_survey_context"},
            },
            {
                "id": "commerce_activity",
                "source": "points[source=commerce]",
                "geometry": "weighted_points",
                "default_visible": True,
                "style": {"type": "heatmap", "color": style_tokens.get("commerce") or "#0EA5E9"},
                "interaction": {"click": "open_order_detail", "filter_key": "source"},
                "privacy": {"customer_pii_redacted": True, "exact_address_redacted": True},
            },
            {
                "id": "geocoding_queue",
                "source": "geocoding.candidates",
                "geometry": "address_rows",
                "default_visible": quality.get("state") == "pending_geocode",
                "style": {"type": "queue_badge", "color": style_tokens.get("attention")},
                "interaction": {"click": "open_geocoding_queue"},
            },
        ],
        "legend_contract": {
            "color_mode": "category_source_quality",
            "show_quality_badge": True,
            "show_ai_badge": True,
            "show_geocoding_badge": True,
        },
    }


def _heatmap_hotspot_actions_contract(
    *,
    hotspots: list[dict[str, Any]],
    quality: dict[str, Any],
    geocoding_candidates: list[dict[str, Any]],
    ai_summary: dict[str, Any],
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for index, hotspot in enumerate(hotspots[:5]):
        actions.append(
            {
                "id": f"inspect_hotspot_{index + 1}",
                "label": "Inspeccionar hotspot",
                "priority": "high" if float(hotspot.get("weight") or 0) >= 1.4 else "medium",
                "action_type": "client_filter",
                "target": {"type": "cell", "cell_id": hotspot.get("id"), "lat": hotspot.get("lat"), "lng": hotspot.get("lng")},
                "ui_hint": "open_heatmap_cell",
                "writes_enabled": False,
            }
        )

    if geocoding_candidates:
        actions.append(
            {
                "id": "open_geocoding_queue",
                "label": "Completar coordenadas pendientes",
                "priority": "high",
                "action_type": "open_queue",
                "target": {"type": "geocoding_queue", "candidate_count": len(geocoding_candidates)},
                "ui_hint": "open_geocoding_queue",
                "href": _crm_tickets_href(focus="open_geocoding_queue", sla="risk"),
                "frontend_path": _crm_tickets_href(focus="open_geocoding_queue", sla="risk"),
                "writes_enabled": False,
            }
        )

    if ai_summary.get("requires_human_attention"):
        actions.append(
            {
                "id": "open_ai_risk_queue",
                "label": "Revisar senales AI de riesgo",
                "priority": "high",
                "action_type": "open_panel",
                "target": {"type": "ai_risk", "risk_level": ai_summary.get("risk_level")},
                "ui_hint": "open_ai_risk_layers",
                "href": _crm_tickets_href(focus="open_ai_risk_layers"),
                "frontend_path": _crm_tickets_href(focus="open_ai_risk_layers"),
                "writes_enabled": False,
            }
        )

    return {
        "contract_version": "operations.heatmap_hotspot_actions.v1",
        "safe_by_default": True,
        "writes_enabled": False,
        "quality_state": quality.get("state"),
        "actions": actions,
        "playbook": [
            {
                "id": "triage_high_density_zone",
                "label": "Priorizar zona de mayor concentracion",
                "trigger": "hotspots_present",
                "enabled": bool(hotspots),
                "steps": ["filter_top_cell", "inspect_records", "assign_owner_or_followup", "monitor_realtime_updates"],
                "confirmation_required_for_writes": True,
            },
            {
                "id": "recover_missing_geolocation",
                "label": "Recuperar ubicaciones faltantes",
                "trigger": "pending_geocode",
                "enabled": bool(geocoding_candidates),
                "steps": ["open_geocoding_queue", "validate_address", "use_update_location_action", "refresh_heatmap"],
                "confirmation_required_for_writes": True,
            },
            {
                "id": "watch_ai_risk_layer",
                "label": "Vigilar capa AI de riesgo",
                "trigger": "ai_requires_attention",
                "enabled": bool(ai_summary.get("requires_human_attention")),
                "steps": ["show_ai_risk_layers", "open_related_records", "escalate_if_confirmed"],
                "confirmation_required_for_writes": True,
            },
        ],
    }


def _heatmap_geocoding_guidance(
    *,
    geocoding_candidates: list[dict[str, Any]],
    location_quality: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    candidate_count = len(geocoding_candidates or [])
    return {
        "contract_version": "operations.heatmap_geocoding_guidance.v1",
        "state": "pending" if candidate_count else "clear",
        "reason_code": "addresses_need_coordinates" if candidate_count else "no_pending_addresses",
        "candidate_count": candidate_count,
        "coverage_percent": location_quality.get("coordinate_coverage_pct"),
        "quality_state": quality.get("state"),
        "backend_external_calls": "none",
        "queue_behavior": "show_candidates_and_require_user_or_batch_confirmation" if candidate_count else "hide_queue_or_show_success",
        "field_contract": {
            "record_id": "string",
            "record_source": "tenant_ticket|municipio_ticket|pyme_ticket",
            "ticket_id": "opaque string or integer serialized losslessly",
            "source_model": "TenantTicket|MunicipioTicket|PymeTicket",
            "address": "string",
            "actions": "array<open_record|update_location>",
            "location_endpoint": "use candidate.actions[id=update_location].endpoint",
            "latitud": "number",
            "longitud": "number",
        },
        "validation_rules": [
            "latitud must be between -90 and 90",
            "longitud must be between -180 and 180",
            "do not mutate records without user confirmation",
            "refresh /api/v2/analytics/operations/heatmap after update_location succeeds",
        ],
        "recommended_actions": [
            {
                "id": "open_geocoding_queue",
                "label": "Abrir cola de geocodificacion",
                "enabled": bool(candidate_count),
                "ui_hint": "open_geocoding_queue",
                "href": _crm_tickets_href(focus="open_geocoding_queue", sla="risk"),
                "frontend_path": _crm_tickets_href(focus="open_geocoding_queue", sla="risk"),
            },
            {
                "id": "request_whatsapp_location",
                "label": "Pedir ubicacion por WhatsApp",
                "enabled": True,
                "ui_hint": "open_template_or_live_chat",
                "href": _crm_tickets_href(focus="open_template_or_live_chat"),
                "frontend_path": _crm_tickets_href(focus="open_template_or_live_chat"),
            },
        ],
    }


def _heatmap_ai_status_contract(ai_insights: dict[str, Any], ai_layers: dict[str, Any]) -> dict[str, Any]:
    hf_status = _as_dict(ai_insights.get("hf_status"))
    frontend_contract = _as_dict(ai_insights.get("frontend_contract"))
    used_hf = bool(hf_status.get("used"))
    return {
        "contract_version": "operations.heatmap_ai_status.v1",
        "provider_family": ai_insights.get("provider_family") or "huggingface",
        "mode": ai_insights.get("mode") or ("huggingface_zero_shot" if used_hf else "deterministic_local_fallback"),
        "status": "hf_active" if used_hf else "local_fallback",
        "configured": bool(hf_status.get("configured")),
        "zero_shot_enabled": bool(hf_status.get("zero_shot_enabled")),
        "used_hf": used_hf,
        "fallback_reason": hf_status.get("fallback_reason"),
        "safe_to_render_without_hf_token": bool(frontend_contract.get("safe_to_render_without_hf_token", True)),
        "ai_layers_ready": bool((ai_layers.get("layers") or {}) if isinstance(ai_layers, dict) else {}),
        "map_layer_hints": (ai_insights.get("summary") or {}).get("map_layer_hints") or [],
        "requires_human_attention": bool((ai_insights.get("summary") or {}).get("requires_human_attention")),
    }


def _lightweight_heatmap_ai_insights(
    *,
    points: list[dict[str, Any]],
    category_layers: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    risk_points = [
        point
        for point in points
        if bool(point.get("overdue"))
        or str(point.get("sla_state") or "").lower() in {"breached", "overdue", "vencido"}
        or float(point.get("weight") or 0) >= 1.5
    ]
    whatsapp_points = [
        point
        for point in points
        if "whatsapp" in str(point.get("channel") or "").lower()
    ]
    survey_points = [point for point in points if point.get("source") == "survey"]
    top_category = (category_layers[0].get("key") if category_layers else None) or "general"
    risk_level = "high" if risk_points else ("medium" if geocoding_candidates else "normal")
    dominant_intent = "survey_or_vote" if len(survey_points) > len(whatsapp_points) else top_category
    requires_human_attention = bool(risk_points or geocoding_candidates)

    return {
        "contract_version": "huggingface.ai_insights.v1",
        "provider_family": "huggingface",
        "mode": "deterministic_lightweight_dashboard",
        "summary": {
            "risk_level": risk_level,
            "dominant_intent": dominant_intent,
            "requires_human_attention": requires_human_attention,
            "map_layer_hints": [
                "ai_risk_pulses",
                "whatsapp_activity",
                "survey_participation",
                "category_heat",
            ],
        },
        "hf_status": {
            "configured": False,
            "zero_shot_enabled": False,
            "used": False,
            "fallback_reason": "lightweight_dashboard_mode",
        },
        "collection": {
            "items_analyzed": len(points) + len(geocoding_candidates),
            "text_items_analyzed": 0,
            "categories": [
                {"key": item.get("key"), "count": int(item.get("count") or 0)}
                for item in category_layers[:8]
            ],
        },
        "recommended_actions": [
            {
                "id": "inspect_top_hotspot" if points else "capture_location_setup",
                "label": "Revisar mapa operativo" if points else "Completar ubicaciones",
                "priority": "high" if risk_level == "high" else "medium",
                "ui_hint": "open_heatmap",
            }
        ],
        "frontend_contract": {
            "recommended_widgets": ["ai_summary_cards", "risk_queue", "map_layer_toggles"],
            "refresh_seconds": 30,
            "safe_to_render_without_hf_token": True,
            "advisory_only": True,
            "lightweight": True,
        },
    }


def _ai_items_from_heatmap(
    records: list[dict[str, Any]],
    points: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for record in records:
        if not _point_matches_filters(_record_to_filter_probe(record), filters):
            continue
        items.append(
            {
                "source": "ticket",
                "record_source": record.get("source"),
                "id": record.get("id"),
                "text": " ".join(
                    str(value)
                    for value in (
                        record.get("title"),
                        record.get("category"),
                        record.get("status"),
                        record.get("priority"),
                        record.get("channel"),
                        record.get("address"),
                    )
                    if value
                ),
                "category": record.get("category"),
                "channel": record.get("channel"),
                "status": record.get("status"),
                "address": record.get("address"),
            }
        )
    for point in points:
        items.append(
            {
                "source": point.get("source"),
                "id": point.get("id"),
                "text": " ".join(
                    str(value)
                    for value in (
                        point.get("label"),
                        point.get("category"),
                        point.get("status"),
                        point.get("channel"),
                    )
                    if value
                ),
                "category": point.get("category"),
                "channel": point.get("channel"),
                "status": point.get("status"),
                "lat": point.get("lat"),
                "lng": point.get("lng"),
            }
        )
    for candidate in geocoding_candidates:
        items.append(
            {
                "source": "pending_geocode",
                "id": candidate.get("id"),
                "text": " ".join(
                    str(value)
                    for value in (
                        candidate.get("label"),
                        candidate.get("category"),
                        candidate.get("status"),
                        candidate.get("channel"),
                        candidate.get("address"),
                    )
                    if value
                ),
                "category": candidate.get("category"),
                "channel": candidate.get("channel"),
                "status": candidate.get("status"),
                "address": candidate.get("address"),
            }
        )
    return items


def _operational_heatmap_ticket_population(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
    ticket_records: list[dict[str, Any]] | None = None,
    segment_filters: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
    """Return the exact ticket population used by the operational heatmap.

    Queue materialization calls this boundary instead of maintaining a second
    query or a looser interpretation of territorial filters.
    """

    filters = segment_filters or {}
    employee_view = is_employee_heatmap_viewer(viewer)
    records = (
        ticket_records
        if ticket_records is not None
        else _collect_ticket_records(tenant, start_date, end_date, viewer=viewer)
    )
    records = filter_ticket_records_for_heatmap(records, viewer)
    jurisdiction = resolve_tenant_jurisdiction(tenant)
    records = [
        {
            **record,
            "coordinate_jurisdiction_status": coordinate_jurisdiction_status(
                record.get("lat"),
                record.get("lng"),
                jurisdiction,
            ),
        }
        for record in records
    ]
    filtered = [
        record
        for record in records
        if _point_matches_filters(_record_to_filter_probe(record), filters)
    ]
    return employee_view, jurisdiction, filtered


def discover_operational_geocoding_queue_candidates(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
    ticket_records: list[dict[str, Any]] | None = None,
    segment_filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Discover durable queue identities without returning source addresses."""

    employee_view = is_employee_heatmap_viewer(viewer)
    if employee_view and employee_heatmap_scope_empty(viewer):
        return {"candidates": [], "discovered": 0, "hidden": 0}

    _employee_view, jurisdiction, records = _operational_heatmap_ticket_population(
        tenant,
        start_date,
        end_date,
        viewer=viewer,
        ticket_records=ticket_records,
        segment_filters=segment_filters,
    )
    source_candidates = [
        record
        for record in records
        if not _record_has_coordinates(record) and bool(record.get("address"))
    ]
    candidates = discover_territorial_geocoding_candidates(
        source_candidates,
        tenant_id=int(tenant.id),
        tenant_slug=str(getattr(tenant, "slug", "") or ""),
        jurisdiction=jurisdiction,
    )
    return {
        "candidates": candidates,
        "discovered": len(source_candidates),
        "hidden": max(0, len(source_candidates) - len(candidates)),
    }


def build_operational_heatmap(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
    ticket_records: list[dict[str, Any]] | None = None,
    max_points: int = 1000,
    segment_filters: dict[str, Any] | None = None,
    include_ai: bool = True,
    bbox: dict[str, float] | None = None,
    commerce_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    filters = segment_filters or {}
    employee_view = is_employee_heatmap_viewer(viewer)
    if employee_view and employee_heatmap_scope_empty(viewer):
        normalized_filters = {
            key: sorted(_normalized_heatmap_filter_values(key, value))
            for key, value in filters.items()
            if _normalized_heatmap_filter_values(key, value)
        }
        return build_employee_aggregated_heatmap(
            tenant,
            start_date,
            end_date,
            scope_empty=True,
            applied_filters=_employee_safe_applied_filters(normalized_filters),
            bbox=bbox,
        )

    employee_view, jurisdiction, filtered_ticket_records = (
        _operational_heatmap_ticket_population(
            tenant,
            start_date,
            end_date,
            viewer=viewer,
            ticket_records=ticket_records,
            segment_filters=filters,
        )
    )
    require_verified_jurisdiction = _tenant_requires_verified_jurisdiction(tenant)
    jurisdiction_verified = _jurisdiction_has_verified_containment(jurisdiction)
    if employee_view:
        # Employee geography is ticket-only. Survey responses, analytics
        # events, and commerce locations have no employee category boundary
        # and therefore must not enter even the k-anonymous aggregation.
        commerce_records = []
    elif commerce_records is None:
        commerce_records, _ = _collect_commerce_records(tenant, start_date, end_date)
    points: list[dict[str, Any]] = []
    jurisdiction_exclusions: Counter = Counter()
    jurisdiction_unverified_exclusions: Counter = Counter()
    if employee_view:
        include_ai = False
    geocoding_candidates = []
    for record in filtered_ticket_records:
        if _record_has_coordinates(record) or not record.get("address"):
            continue
        candidate_payload = _geocoding_candidate(record)
        durable_candidate = build_territorial_geocoding_candidate(
            record,
            tenant_id=int(tenant.id),
            tenant_slug=str(getattr(tenant, "slug", "") or ""),
            jurisdiction=jurisdiction,
        )
        if durable_candidate is not None:
            candidate_payload["queue_identity"] = durable_candidate.audit_identity()
            candidate_payload["processing_contract"] = {
                "contract_version": "operations.territorial_geocoding.v1",
                "default_mode": "dry_run",
                "provider_execution_enabled": False,
                "writes_enabled": False,
                "states": ["pending", "needs_review", "applied", "failed"],
                "requires": [
                    "tenant_jurisdiction",
                    "provider_opt_in",
                    "writer_authority",
                    "idempotency_key",
                ],
            }
        geocoding_candidates.append(candidate_payload)
    reported_location_review_candidates = [
        _reported_location_review_candidate(record)
        for record in filtered_ticket_records
        if record.get("reported_location_text")
    ]
    territorial_facets = (
        build_territorial_facets(
            filtered_ticket_records,
            require_verified_jurisdiction=require_verified_jurisdiction,
            jurisdiction_verified=jurisdiction_verified,
        )
        if not employee_view
        else None
    )

    for record in filtered_ticket_records:
        if record.get("lat") is None or record.get("lng") is None:
            continue
        jurisdiction_status = str(record.get("coordinate_jurisdiction_status") or "missing")
        exclusion_bucket = _jurisdiction_exclusion_bucket(
            jurisdiction_status,
            require_verified_jurisdiction=require_verified_jurisdiction,
            jurisdiction_verified=jurisdiction_verified,
        )
        if exclusion_bucket:
            if exclusion_bucket == "outside":
                jurisdiction_exclusions["tickets"] += 1
            else:
                jurisdiction_unverified_exclusions["tickets"] += 1
            continue
        demographics = _as_dict(record.get("demographics"))
        point = {
            "layer": "tickets",
            "source": "ticket",
            "record_source": record["source"],
            "source_model": record["source_model"],
            "ticket_id": record["id"],
            "id": f"{record['source']}:{record['id']}",
            "lat": float(record["lat"]),
            "lng": float(record["lng"]),
            "weight": _priority_weight(record["priority"], record["status"]),
            "category": record["category"],
            "raw_category": record.get("raw_category"),
            "category_provenance": record.get("category_provenance"),
            "channel": record["channel"],
            "status": record["status"],
            "sla_state": record.get("sla_state") or "normal",
            "overdue": bool(record.get("overdue")),
            "assignee_id": record.get("assignee_id"),
            "zone": record.get("zone"),
            "address": record.get("address"),
            "location_provenance": record.get("location_provenance"),
            "label": record["title"],
            "timestamp": _iso(record.get("created_at")),
            "gender": demographics.get("gender") or "unknown",
            "age_range": demographics.get("age_range") or "unknown",
            "demographics_source": demographics.get("source") or "missing",
            "actions": _ticket_action_contract(record),
            "coordinate_jurisdiction_status": jurisdiction_status,
        }
        if _point_matches_bbox(point, bbox):
            points.append(point)

    survey_responses = []
    survey_response_provenance = build_survey_response_provenance(
        real_count=0,
        synthetic_count=0,
        mode="real",
    )
    if not employee_view:
        survey_response_query = _between(
            EncRespuesta.query.filter_by(tenant_id=tenant.id),
            EncRespuesta.submitted_at,
            start_date,
            end_date,
        ).filter(
            EncRespuesta.lat.isnot(None),
            EncRespuesta.lng.isnot(None),
        )
        response_counts_by_origin = {
            str(origin or ""): int(count or 0)
            for origin, count in (
                survey_response_query.with_entities(
                    EncRespuesta.response_origin,
                    func.count(EncRespuesta.id),
                )
                .group_by(EncRespuesta.response_origin)
                .all()
            )
        }
        real_survey_response_count = response_counts_by_origin.get(
            SURVEY_RESPONSE_ORIGIN_REAL,
            0,
        )
        synthetic_survey_response_count = response_counts_by_origin.get(
            SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
            0,
        )
        unverified_survey_response_count = response_counts_by_origin.get(
            SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
            0,
        )
        survey_responses = (
            survey_response_query.filter(
                EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL
            )
            .order_by(EncRespuesta.submitted_at.desc(), EncRespuesta.id.desc())
            .limit(max_points)
            .all()
        )
        survey_response_provenance = build_survey_response_provenance(
            real_count=real_survey_response_count,
            synthetic_count=synthetic_survey_response_count,
            unverified_count=unverified_survey_response_count,
            mode="real",
        )
    for response in survey_responses:
        metadata = _as_dict(response.metadata_payload)
        demographics = _demographics_from_metadata(
            {
                **metadata,
                "genero": response.genero,
                "edad": response.edad,
                "rango_etario": response.rango_etario,
                "anio_nacimiento": response.anio_nacimiento,
            }
        )
        point = {
            "layer": "surveys",
            "source": "survey",
            "record_source": "survey_response",
            "id": f"survey_response:{response.id}",
            "lat": float(response.lat),
            "lng": float(response.lng),
            "weight": 0.8,
            "category": _norm(metadata.get("categoria") or metadata.get("category"), "survey_response"),
            "channel": _norm(response.canal, "survey"),
            "status": "submitted",
            "label": response.barrio or response.ciudad or "Respuesta de encuesta",
            "timestamp": _iso(response.submitted_at),
            "gender": demographics.get("gender") or "unknown",
            "age_range": demographics.get("age_range") or "unknown",
            "demographics_source": demographics.get("source") or "missing",
        }
        if _point_matches_filters(point, filters):
            jurisdiction_status = coordinate_jurisdiction_status(point.get("lat"), point.get("lng"), jurisdiction)
            point["coordinate_jurisdiction_status"] = jurisdiction_status
            exclusion_bucket = _jurisdiction_exclusion_bucket(
                jurisdiction_status,
                require_verified_jurisdiction=require_verified_jurisdiction,
                jurisdiction_verified=jurisdiction_verified,
            )
            if exclusion_bucket:
                if exclusion_bucket == "outside":
                    jurisdiction_exclusions["surveys"] += 1
                else:
                    jurisdiction_unverified_exclusions["surveys"] += 1
            elif _point_matches_bbox(point, bbox):
                points.append(point)

    events = []
    if not employee_view:
        events = _between(
            AnalyticsEventV2.query.filter_by(tenant_id=tenant.id),
            AnalyticsEventV2.ts,
            start_date,
            end_date,
        ).filter(
            AnalyticsEventV2.lat.isnot(None),
            AnalyticsEventV2.lng.isnot(None),
        ).limit(max_points).all()
    for event in events:
        metadata = _as_dict(event.metadata_payload)
        demographics = _demographics_from_metadata(metadata)
        point = {
            "layer": "analytics_events",
            "source": "analytics_event",
            "record_source": "analytics_event",
            "id": f"analytics_event:{event.id}",
            "lat": float(event.lat),
            "lng": float(event.lng),
            "weight": 0.6,
            "category": _norm(metadata.get("categoria") or metadata.get("category") or event.event_name, "event"),
            "channel": _norm(event.channel, "unknown"),
            "status": "event",
            "label": event.event_name,
            "timestamp": _iso(event.ts),
            "gender": demographics.get("gender") or "unknown",
            "age_range": demographics.get("age_range") or "unknown",
            "demographics_source": demographics.get("source") or "missing",
        }
        if _point_matches_filters(point, filters):
            jurisdiction_status = coordinate_jurisdiction_status(point.get("lat"), point.get("lng"), jurisdiction)
            point["coordinate_jurisdiction_status"] = jurisdiction_status
            exclusion_bucket = _jurisdiction_exclusion_bucket(
                jurisdiction_status,
                require_verified_jurisdiction=require_verified_jurisdiction,
                jurisdiction_verified=jurisdiction_verified,
            )
            if exclusion_bucket:
                if exclusion_bucket == "outside":
                    jurisdiction_exclusions["analytics_events"] += 1
                else:
                    jurisdiction_unverified_exclusions["analytics_events"] += 1
            elif _point_matches_bbox(point, bbox):
                points.append(point)

    for order in commerce_records:
        location = _as_dict(order.get("location"))
        lat = _float_or_none(location.get("lat"))
        lng = _float_or_none(location.get("lng"))
        if lat is None or lng is None:
            continue
        order_id = str(order.get("id") or "")
        record_id = order.get("source_id")
        request_kind = _norm(order.get("request_kind"), "commerce_order")
        point = {
            "layer": "commerce_activity",
            "source": "commerce",
            "record_source": _norm(order.get("source_model"), "order"),
            "id": order_id,
            "lat": lat,
            "lng": lng,
            "weight": 0.9 if _norm(order.get("status"), "") not in _CLOSED_STATES else 0.5,
            "category": request_kind,
            "channel": _norm(order.get("channel"), "unknown"),
            "status": _norm(order.get("status"), "unknown"),
            "label": "Pedido comercial",
            "timestamp": order.get("updated_at") or order.get("created_at"),
            "gender": "unknown",
            "age_range": "unknown",
            "demographics_source": "not_collected",
            "privacy": {
                "customer_pii_redacted": True,
                "exact_address_redacted": True,
                "coordinate_source": "order_fulfillment",
            },
            "actions": [
                {
                    "id": "open_order",
                    "label": "Abrir pedido",
                    "method": "GET",
                    "endpoint": f"/api/admin/tenants/{tenant.slug}/orders/{quote(order_id, safe=':')}",
                    "href": f"/perfil?tab=pedidos&order_id={quote(order_id, safe='')}",
                    "frontend_path": f"/perfil?tab=pedidos&order_id={quote(order_id, safe='')}",
                    "ui_hint": "open_order_detail",
                    "writes_enabled": False,
                }
            ],
        }
        if _point_matches_filters(point, filters):
            jurisdiction_status = coordinate_jurisdiction_status(point.get("lat"), point.get("lng"), jurisdiction)
            point["coordinate_jurisdiction_status"] = jurisdiction_status
            exclusion_bucket = _jurisdiction_exclusion_bucket(
                jurisdiction_status,
                require_verified_jurisdiction=require_verified_jurisdiction,
                jurisdiction_verified=jurisdiction_verified,
            )
            if exclusion_bucket:
                if exclusion_bucket == "outside":
                    jurisdiction_exclusions["commerce"] += 1
                else:
                    jurisdiction_unverified_exclusions["commerce"] += 1
            elif _point_matches_bbox(point, bbox):
                points.append(point)

    points = [
        _attach_point_jurisdiction_evidence(point, jurisdiction)
        for point in points[:max_points]
    ]
    cells: dict[str, dict[str, Any]] = {}
    recent_cutoff = (_aware_datetime(end_date) or datetime.now(timezone.utc)) - timedelta(hours=24)
    for point in points:
        cell_id = f"{round(point['lat'], 3)}:{round(point['lng'], 3)}"
        cell = cells.setdefault(
            cell_id,
            {
                "id": cell_id,
                "lat": round(point["lat"], 3),
                "lng": round(point["lng"], 3),
                "weight": 0.0,
                "count": 0,
                "sources": Counter(),
                "categories": Counter(),
                "channels": Counter(),
                "genders": Counter(),
                "age_ranges": Counter(),
                "statuses": Counter(),
                "sla_states": Counter(),
                "ticket_count": 0,
                "survey_count": 0,
                "event_count": 0,
                "commerce_count": 0,
                "overdue_count": 0,
                "unassigned_count": 0,
                "breached_sla_count": 0,
                "recent_24h_count": 0,
                "latest_event_at": None,
            },
        )
        source = _norm(point.get("source"), "unknown")
        status = _norm(point.get("status"), "unknown")
        sla_state = _norm(point.get("sla_state"), "normal")
        parsed_timestamp = _parse_datetime(point.get("timestamp"))
        cell["weight"] += float(point.get("weight") or 1.0)
        cell["count"] += 1
        cell["sources"][source] += 1
        cell["categories"][point["category"]] += 1
        cell["channels"][point.get("channel") or "unknown"] += 1
        cell["genders"][point.get("gender") or "unknown"] += 1
        cell["age_ranges"][point.get("age_range") or "unknown"] += 1
        cell["statuses"][status] += 1
        cell["sla_states"][sla_state] += 1
        if source == "ticket":
            cell["ticket_count"] += 1
            is_open_ticket = status not in _CLOSED_STATES
            if is_open_ticket and not point.get("assignee_id"):
                cell["unassigned_count"] += 1
            if bool(point.get("overdue")):
                cell["overdue_count"] += 1
            if bool(point.get("overdue")) or sla_state in _OVERDUE_STATES:
                cell["breached_sla_count"] += 1
        elif source == "survey":
            cell["survey_count"] += 1
        elif source == "analytics_event":
            cell["event_count"] += 1
        elif source == "commerce":
            cell["commerce_count"] += 1
        if parsed_timestamp:
            if parsed_timestamp >= recent_cutoff:
                cell["recent_24h_count"] += 1
            cell["latest_event_at"] = _max_datetime(cell.get("latest_event_at"), parsed_timestamp)

    cell_items = []
    for cell in cells.values():
        cell_items.append(
            {
                "id": cell["id"],
                "lat": cell["lat"],
                "lng": cell["lng"],
                "weight": round(cell["weight"], 2),
                "count": cell["count"],
                "sources": _counter(cell["sources"], limit=5),
                "top_categories": _counter(cell["categories"], limit=5),
                "top_channels": _counter(cell["channels"], limit=5),
                "top_statuses": _counter(cell["statuses"], limit=5),
                "top_sla_states": _counter(cell["sla_states"], limit=5),
                "ticket_count": int(cell["ticket_count"]),
                "survey_count": int(cell["survey_count"]),
                "event_count": int(cell["event_count"]),
                "commerce_count": int(cell["commerce_count"]),
                "overdue_count": int(cell["overdue_count"]),
                "unassigned_count": int(cell["unassigned_count"]),
                "breached_sla_count": int(cell["breached_sla_count"]),
                "recent_24h_count": int(cell["recent_24h_count"]),
                "latest_event_at": _iso(cell.get("latest_event_at")),
                "demographics": {
                    "gender": _segment_items(cell["genders"], limit=5),
                    "age_ranges": _segment_items(cell["age_ranges"], limit=5),
                },
            }
        )
    cell_items.sort(key=lambda item: (item["weight"], item["count"]), reverse=True)
    hotspots = cell_items[:10]
    operational_hotspots = _heatmap_operational_hotspots(cell_items)

    category_counter = Counter(point.get("category") or "unknown" for point in points)
    gender_counter = Counter(point.get("gender") or "unknown" for point in points)
    age_range_counter = Counter(point.get("age_range") or "unknown" for point in points)
    channel_counter = Counter(point.get("channel") or "unknown" for point in points)
    source_counter = Counter(point.get("source") or "unknown" for point in points)
    status_counter = Counter(point.get("status") or "unknown" for point in points)
    zone_counter = Counter(
        zone
        for point in points
        for zone in [explicit_zone(point.get("zone"))]
        if zone
    )
    sla_counter = Counter(point.get("sla_state") or "normal" for point in points)

    category_layers = []
    for category, count in category_counter.most_common(20):
        category_points = [point for point in points if point.get("category") == category]
        category_layers.append(
            {
                "key": category,
                "label": category,
                "count": int(count),
                "weight": round(sum(float(point.get("weight") or 1.0) for point in category_points), 2),
                "points": category_points[:100],
            }
        )

    bounds = None
    if points:
        lats = [point["lat"] for point in points]
        lngs = [point["lng"] for point in points]
        bounds = {"north": max(lats), "south": min(lats), "east": max(lngs), "west": min(lngs)}

    normalized_filters = {
        key: sorted(_normalized_heatmap_filter_values(key, value))
        for key, value in filters.items()
        if _normalized_heatmap_filter_values(key, value)
    }
    points_with_gender = len([point for point in points if point.get("gender") not in (None, "unknown")])
    points_with_age = len([point for point in points if point.get("age_range") not in (None, "unknown")])
    location_quality = _location_quality(
        filtered_ticket_records,
        geocoding_candidates,
        require_verified_jurisdiction=require_verified_jurisdiction,
        jurisdiction_verified=jurisdiction_verified,
    )
    jurisdiction_review_candidates = []
    for record in filtered_ticket_records:
        if not _record_has_coordinates(record):
            continue
        reason_code = _jurisdiction_review_reason_code(
            str(record.get("coordinate_jurisdiction_status") or "missing"),
            require_verified_jurisdiction=require_verified_jurisdiction,
            jurisdiction_verified=jurisdiction_verified,
        )
        if reason_code:
            jurisdiction_review_candidates.append(
                {**_geocoding_candidate(record), "reason_code": reason_code}
            )
    boundary_unavailable = bool(
        require_verified_jurisdiction
        and not jurisdiction_verified
        and sum(jurisdiction_unverified_exclusions.values()) > 0
    )
    verified_boundary_collection = (
        jurisdiction.get("boundary_feature_collection")
        if jurisdiction_verified
        else None
    )
    public_jurisdiction = {
        key: value
        for key, value in jurisdiction.items()
        if key not in {"boundary_geometry", "boundary_feature_collection"}
    }
    map_eligibility_state = (
        "verified"
        if jurisdiction_verified
        else ("blocked" if require_verified_jurisdiction else "eligible")
    )
    map_eligibility_reason_code = (
        "official_point_in_polygon_verified"
        if jurisdiction_verified
        else (
            "official_jurisdiction_boundary_unavailable"
            if require_verified_jurisdiction
            else "verified_jurisdiction_not_required"
        )
    )
    jurisdiction = {
        **public_jurisdiction,
        "map_eligibility_state": map_eligibility_state,
        "map_eligibility_reason_code": map_eligibility_reason_code,
        "excluded_coordinate_records": int(
            sum(jurisdiction_exclusions.values()) + sum(jurisdiction_unverified_exclusions.values())
        ),
        "excluded_by_source": _counter(jurisdiction_exclusions),
        "unverified_coordinate_records": int(sum(jurisdiction_unverified_exclusions.values())),
        "unverified_by_source": _counter(jurisdiction_unverified_exclusions),
        "review_candidate_count": len(jurisdiction_review_candidates),
    }
    quality = _heatmap_quality_contract(
        points=points,
        records=filtered_ticket_records,
        geocoding_candidates=geocoding_candidates,
        location_quality=location_quality,
        max_points=max_points,
        jurisdiction_blocked=boundary_unavailable,
    )
    realtime = _heatmap_realtime_contract(points)
    if include_ai:
        ai_insights = build_collection_ai_insights(
            _ai_items_from_heatmap(filtered_ticket_records, points, geocoding_candidates, filters),
            domain="operations",
        )
    else:
        ai_insights = _lightweight_heatmap_ai_insights(
            points=points,
            category_layers=category_layers,
            geocoding_candidates=geocoding_candidates,
        )
    ai_layers = build_map_ai_layers(points, category_layers=category_layers, insights=ai_insights)
    ai_summary = ai_insights.get("summary") or {}
    heatmap_summary = {
        "points": len(points),
        "cells": len(cell_items),
        "operational_hotspots": len(operational_hotspots),
        "can_render_heatmap": bool(points),
        "ticket_points": len([point for point in points if point["source"] == "ticket"]),
        "survey_points": len([point for point in points if point["source"] == "survey"]),
        "event_points": len([point for point in points if point["source"] == "analytics_event"]),
        "commerce_points": len([point for point in points if point["source"] == "commerce"]),
        "points_with_gender": points_with_gender,
        "points_with_age": points_with_age,
        "unknown_gender_points": len(points) - points_with_gender,
        "unknown_age_points": len(points) - points_with_age,
        "filtered": bool(normalized_filters),
        "pending_geocode": len(geocoding_candidates),
        "coordinate_coverage_pct": location_quality.get("coordinate_coverage_pct"),
        "outside_jurisdiction": location_quality.get("ticket_records_outside_jurisdiction"),
        "coverage_rate": quality.get("coverage_rate"),
        "coverage_percent": quality.get("coverage_percent"),
        "quality_state": quality.get("state"),
        "quality_reason_code": quality.get("reason_code"),
        "ai_risk_level": ai_summary.get("risk_level") or "normal",
        "dominant_intent": ai_summary.get("dominant_intent") or "general_query",
        "requires_human_attention": bool(ai_summary.get("requires_human_attention")),
    }
    geocoding_guidance = _heatmap_geocoding_guidance(
        geocoding_candidates=geocoding_candidates,
        location_quality=location_quality,
        quality=quality,
    )
    hotspot_actions = _heatmap_hotspot_actions_contract(
        hotspots=hotspots,
        quality=quality,
        geocoding_candidates=geocoding_candidates,
        ai_summary=ai_summary,
    )
    viewport_presets = _heatmap_viewport_presets(
        bounds=bounds,
        hotspots=hotspots,
        quality=quality,
        geocoding_candidates=geocoding_candidates,
    )
    layer_style_contract = _heatmap_layer_style_contract(
        ai_layers=ai_layers,
        category_layers=category_layers,
        quality=quality,
    )
    map_narrative = _heatmap_narrative_contract(
        tenant,
        summary=heatmap_summary,
        quality=quality,
        category_layers=category_layers,
        geocoding_candidates=geocoding_candidates,
        ai_summary=ai_summary,
    )
    ai_status = _heatmap_ai_status_contract(ai_insights, ai_layers)
    source_quality = _heatmap_source_quality(
        points=points,
        records=filtered_ticket_records,
        geocoding_candidates=geocoding_candidates,
        commerce_records=commerce_records,
    )
    geo_layers = _heatmap_geo_layers(
        points=points,
        cells=cell_items,
        hotspots=hotspots,
        category_layers=category_layers,
        boundaries=verified_boundary_collection,
    )
    map_layers = _heatmap_map_layers(
        geo_layers=geo_layers,
        category_layers=category_layers,
        source_quality=source_quality,
    )

    payload = {
        "contract_version": "operations.heatmap.v1",
        "tenant": _tenant_ref(tenant),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "render_contract": {
            "state": "ready" if points else "empty",
            "can_render_heatmap": bool(points),
            "empty_reason": (
                None
                if points
                else (
                    "official_jurisdiction_boundary_unavailable"
                    if boundary_unavailable
                    else "no_real_geo_points"
                )
            ),
            "map_engine": "maplibre",
            "layers": ["tickets", "surveys", "analytics_events", "commerce_activity", "ai_risk", "whatsapp_activity"],
            "point_format": {"lat": "number", "lng": "number", "weight": "number"},
            "segment_filters": [
                "categoria",
                "estado",
                "genero",
                "rango_edad",
                "source",
                "channel",
                "zona",
                "direccion",
                "corredor",
                "sla_state",
                "assignee_id",
            ],
            "category_layers": True,
            "demographics_source": "metadata_fields_only",
            "address_geocoding": True,
            "geocoding_state": (location_quality.get("reason_code") or "unknown"),
            "quality_state": quality.get("state"),
            "quality_reason_code": quality.get("reason_code"),
            "recommended_views": [
                "heatmap",
                "category_layers",
                "demographic_segments",
                "geocoding_queue",
                "interactive_globe",
                "ai_risk_layers",
                "whatsapp_activity_layer",
                "survey_participation_layer",
                "commerce_activity_layer",
            ],
            "premium_metadata": [
                "map_narrative",
                "viewport_presets",
                "layer_style_contract",
                "hotspot_actions",
                "operational_hotspots",
                "geo_layers",
                "map_layers",
                "source_quality",
                "geocoding.guidance",
                "ai_status",
            ],
        },
        "summary": heatmap_summary,
        "map_narrative": map_narrative,
        "viewport_presets": viewport_presets,
        "layer_style_contract": layer_style_contract,
        "hotspot_actions": hotspot_actions,
        "ai_status": ai_status,
        "ai_insights": ai_insights,
        "ai_layers": ai_layers,
        "source_quality": source_quality,
        "geo_layers": geo_layers,
        "map_layers": map_layers,
        "map_experience": {
            "contract_version": "operations.map_experience.v1",
            "preferred_visualization": "interactive_globe_heatmap",
            "map_engines": ["maplibre", "deckgl", "google"],
            "layer_groups": ["base_heatmap", "category_layers", "ai_risk_layers", "whatsapp_activity", "survey_participation", "commerce_activity"],
            "empty_state_behavior": "show_geocoding_queue_and_ai_summary",
            "supports_reduced_motion": True,
            "narrative_contract": map_narrative.get("contract_version"),
            "viewport_contract": viewport_presets.get("contract_version"),
            "layer_style_contract": layer_style_contract.get("contract_version"),
            "hotspot_actions_contract": hotspot_actions.get("contract_version"),
            "ai_status_contract": ai_status.get("contract_version"),
        },
        "quality": quality,
        "realtime": realtime,
        "legend": {
            "mode": "category_source_quality",
            "categories": [
                {"key": item["key"], "label": item["label"], "count": item["count"]}
                for item in category_layers[:12]
            ],
            "sources": _segment_items(source_counter),
            "quality": {
                "state": quality.get("state"),
                "coverage_percent": quality.get("coverage_percent"),
            },
        },
        "applied_filters": normalized_filters,
        "spatial_filter": {
            "bbox": bbox,
            "applied": bool(bbox),
        },
        "segments": {
            "category": _segment_items(category_counter),
            "gender": _segment_items(gender_counter),
            "age_range": _segment_items(age_range_counter),
            "channel": _segment_items(channel_counter),
            "source": _segment_items(source_counter),
            "status": _segment_items(status_counter),
            "zone": _segment_items(zone_counter),
            "sla_state": _segment_items(sla_counter),
        },
        "demographics": {
            "source": "real_metadata_only",
            "gender": _segment_items(gender_counter),
            "age_ranges": _segment_items(age_range_counter),
            "known_gender_points": points_with_gender,
            "known_age_points": points_with_age,
            "unknown_gender_points": len(points) - points_with_gender,
            "unknown_age_points": len(points) - points_with_age,
        },
        "location_quality": location_quality,
        "jurisdiction": jurisdiction,
        "jurisdiction_review": {
            "contract_version": "operations.heatmap.jurisdiction_review.v1",
            "status": "pending" if jurisdiction_review_candidates else "empty",
            "reason_code": (
                "official_jurisdiction_boundary_unavailable"
                if boundary_unavailable
                else (
                    "coordinates_outside_configured_jurisdiction"
                    if jurisdiction_review_candidates
                    else "no_outside_coordinates"
                )
            ),
            "candidate_count": len(jurisdiction_review_candidates),
            "candidates": jurisdiction_review_candidates[:50],
            "writes_performed": False,
        },
        "geocoding": {
            "contract_version": "operations.heatmap.geocoding_queue.v1",
            "status": "pending" if geocoding_candidates else "empty",
            "reason_code": "address_without_coordinates" if geocoding_candidates else "no_pending_addresses",
            "candidate_count": len(geocoding_candidates),
            "candidates": geocoding_candidates[:50],
            "guidance": geocoding_guidance,
            "execution": {
                "service_contract": "operations.territorial_geocoding.v1",
                "default_mode": "dry_run",
                "provider_execution_enabled": False,
                "writes_enabled": False,
                "audit_storage": "territorial_geocoding_job+attempt",
            },
            "recommended_action": {
                "action_id": "geocode_ticket_addresses",
                "label": "Geocodificar direcciones pendientes",
                "method": "dynamic",
                "endpoint_template": "use candidates[].actions[id=update_location].endpoint",
                "requires": ["latitud|location.lat", "longitud|location.lng"],
                "body_template": "use candidates[].actions[id=update_location].body_template",
            },
        },
        "category_layers": category_layers,
        "bounds": bounds,
        "points": points,
        "cells": cell_items,
        "hotspots": hotspots,
        "operational_hotspots": operational_hotspots,
        "ui": {
            "labels": {
                "map_quality": "Calidad del mapa",
                "coverage": "Cobertura GPS",
                "visible_points": "Puntos visibles",
                "pending_geocode": "Pendientes de geocodificar",
                "realtime": "Actualizacion en vivo",
                "quality_ready": "Mapa operativo confiable",
                "quality_partial": "Cobertura territorial parcial",
                "quality_pending_geocode": "Direcciones pendientes de geocodificar",
                "quality_blocked": "Sin coordenadas reales",
                "quality_empty": "Sin eventos para el periodo",
            }
        },
        "response_provenance": survey_response_provenance,
    }

    if employee_view:
        return build_employee_aggregated_heatmap(
            tenant,
            start_date,
            end_date,
            exact_payload=payload,
            applied_filters=_employee_safe_applied_filters(
                payload.get("applied_filters") or {}
            ),
            bbox=bbox,
        )

    payload["territorial_facets"] = territorial_facets
    payload["location_review"] = {
        "contract_version": "operations.heatmap.location_review.v1",
        "status": "pending" if reported_location_review_candidates else "empty",
        "reason_code": (
            "reported_location_text_requires_review"
            if reported_location_review_candidates
            else "no_reported_location_text_pending_review"
        ),
        "candidate_count": len(reported_location_review_candidates),
        "candidates": reported_location_review_candidates[:50],
        "automatic_geocoding": False,
        "writes_performed": False,
    }
    payload["render_contract"]["premium_metadata"].append("territorial_facets")
    payload["render_contract"]["premium_metadata"].append("location_review")
    payload["render_contract"]["premium_metadata"].append("jurisdiction")
    payload["privacy"] = privileged_heatmap_privacy()
    return payload


def _build_alerts(
    ticket_metrics: dict[str, Any],
    survey_metrics: dict[str, Any],
    chat_metrics: dict[str, Any],
    employee_metrics: dict[str, Any],
    heatmap: dict[str, Any],
    commerce_metrics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    ticket_summary = ticket_metrics.get("summary") or {}
    survey_summary = survey_metrics.get("summary") or {}
    chat_summary = chat_metrics.get("summary") or {}
    employee_summary = employee_metrics.get("summary") or {}
    commerce_summary = (commerce_metrics or {}).get("summary") or {}
    survey_available = survey_metrics.get("available", True) is not False
    chat_available = chat_metrics.get("available", True) is not False
    employee_available = employee_metrics.get("available", True) is not False
    commerce_available = (commerce_metrics or {}).get("available", True) is not False

    if ticket_summary.get("overdue", 0) > 0:
        alerts.append({"severity": "high", "reason_code": "tickets_overdue", "message": "Hay tickets o reclamos vencidos que requieren accion."})
    if ticket_summary.get("unassigned", 0) > 0:
        alerts.append({"severity": "medium", "reason_code": "tickets_unassigned", "message": "Hay tickets abiertos sin responsable asignado."})
    if (
        employee_available
        and ticket_summary.get("whatsapp", 0) > 0
        and employee_summary.get("employees", 0) == 0
    ):
        alerts.append({"severity": "high", "reason_code": "whatsapp_without_team", "message": "Entraron reclamos por WhatsApp pero no hay empleados configurados."})
    if survey_available and survey_summary.get("votaciones_live", 0) > 0 and survey_summary.get("responses", 0) == 0:
        alerts.append({"severity": "medium", "reason_code": "live_vote_without_responses", "message": "Hay votaciones activas sin respuestas recientes."})
    if chat_available and chat_summary.get("handoff_rate", 0) >= 30:
        alerts.append({"severity": "medium", "reason_code": "high_handoff_rate", "message": "La tasa de derivacion humana esta alta; revisar intents y respuestas."})
    if commerce_available and int(commerce_summary.get("orders_needing_review") or 0) > 0:
        alerts.append({
            "severity": "medium",
            "reason_code": "assisted_orders_need_review",
            "message": "Hay pedidos asistidos desde WhatsApp o marketplace esperando revision operativa.",
        })
    hotspot = (heatmap.get("hotspots") or [None])[0]
    if hotspot and hotspot.get("count", 0) >= 5:
        alerts.append({"severity": "medium", "reason_code": "heatmap_hotspot", "message": "Se detecto una zona con alta concentracion de actividad.", "cell_id": hotspot.get("id")})

    return alerts


def _summary_from_metrics(
    ticket_metrics: dict[str, Any],
    survey_metrics: dict[str, Any],
    chat_metrics: dict[str, Any],
    employee_metrics: dict[str, Any],
    heatmap: dict[str, Any],
    alerts: list[dict[str, Any]] | None = None,
    commerce_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commerce_summary = (commerce_metrics or {}).get("summary") or {}
    return {
        "open_tickets": ticket_metrics["summary"]["open"],
        "overdue_tickets": ticket_metrics["summary"]["overdue"],
        "survey_responses": survey_metrics["summary"]["responses"],
        "live_votes": survey_metrics["summary"]["votaciones_live"],
        "chat_messages": chat_metrics["summary"]["messages"],
        "whatsapp_messages": chat_metrics["summary"]["whatsapp_messages"],
        "orders": commerce_summary.get("orders", 0),
        "assisted_orders": commerce_summary.get("assisted_orders", 0),
        "orders_needing_review": commerce_summary.get("orders_needing_review", 0),
        "unmatched_order_items": commerce_summary.get("unmatched_items", 0),
        "employees": employee_metrics["summary"]["employees"],
        "heatmap_points": heatmap["summary"]["points"],
        "alerts": len(alerts or []),
    }


def _build_trends(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "survey_responses",
        "live_votes",
        "chat_messages",
        "whatsapp_messages",
        "orders",
        "assisted_orders",
        "orders_needing_review",
        "heatmap_points",
    ]
    return {
        "contract_version": "operations.trends.v1",
        "items": [_trend_item(key, current.get(key, 0), previous.get(key, 0)) for key in keys],
        "unavailable": [
            {
                "key": "open_tickets",
                "reason_code": "point_in_time_snapshot_has_no_historical_ledger",
            },
            {
                "key": "overdue_tickets",
                "reason_code": "point_in_time_snapshot_has_no_historical_ledger",
            },
        ],
    }


def _action(
    *,
    action_id: str,
    title: str,
    description: str,
    priority: str,
    reason_code: str,
    endpoint: str,
    method: str = "GET",
    payload_template: dict[str, Any] | None = None,
    ui_hint: str = "open_view",
    href: str | None = None,
) -> dict[str, Any]:
    frontend_path = href or _crm_tickets_href(focus=ui_hint)
    return {
        "id": action_id,
        "title": title,
        "description": description,
        "priority": priority,
        "reason_code": reason_code,
        "endpoint": endpoint,
        "method": method,
        "href": frontend_path,
        "frontend_path": frontend_path,
        "payload_template": payload_template or {},
        "ui_hint": ui_hint,
    }


def _build_next_best_actions(
    ticket_metrics: dict[str, Any],
    survey_metrics: dict[str, Any],
    chat_metrics: dict[str, Any],
    employee_metrics: dict[str, Any],
    heatmap: dict[str, Any],
    alerts: list[dict[str, Any]],
    commerce_metrics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    ticket_summary = ticket_metrics.get("summary") or {}
    survey_summary = survey_metrics.get("summary") or {}
    chat_summary = chat_metrics.get("summary") or {}
    employee_summary = employee_metrics.get("summary") or {}
    commerce_summary = (commerce_metrics or {}).get("summary") or {}
    coverage = employee_metrics.get("coverage") or {}
    survey_available = survey_metrics.get("available", True) is not False
    chat_available = chat_metrics.get("available", True) is not False
    employee_available = employee_metrics.get("available", True) is not False
    commerce_available = (commerce_metrics or {}).get("available", True) is not False

    if ticket_summary.get("overdue", 0) > 0:
        actions.append(
            _action(
                action_id="review_overdue_tickets",
                title="Revisar tickets vencidos",
                description="Priorizar reclamos y tickets con SLA vencido antes de que escalen.",
                priority="high",
                reason_code="tickets_overdue",
                endpoint="/api/v2/tickets?status=overdue",
                href=_crm_tickets_href(focus="open_overdue_queue", status="overdue", sla="risk"),
            )
        )

    if ticket_summary.get("unassigned", 0) > 0:
        actions.append(
            _action(
                action_id="assign_unassigned_tickets",
                title="Asignar tickets sin responsable",
                description="Hay tickets abiertos sin empleado asignado. Conviene resolver ownership primero.",
                priority="high" if ticket_summary.get("unassigned", 0) >= 5 else "medium",
                reason_code="tickets_unassigned",
                endpoint="/api/v2/inbox/omnichannel/actions",
                method="POST",
                payload_template={
                    "action": "assign",
                    "source_model": "{source_model}",
                    "ticket_id": "{ticket_id}",
                    "assignee_id": "{employee_id}",
                    "expected_assignee_id": "{expected_assignee_id}",
                },
                ui_hint="open_assignment_drawer",
                href=_crm_tickets_href(focus="open_assignment_drawer", sla="risk"),
            )
        )

    if employee_available and (
        coverage.get("uncovered_categories") or coverage.get("uncovered_channels")
    ):
        actions.append(
            _action(
                action_id="improve_employee_coverage",
                title="Completar cobertura de empleados",
                description="Hay categorias o canales sin cobertura declarada para la demanda actual.",
                priority="medium",
                reason_code="employee_coverage_gap",
                endpoint="/api/v2/employee-coverage",
                ui_hint="open_employee_coverage",
                href="/perfil?tab=empleados&focus=coverage",
            )
        )

    if survey_available and survey_summary.get("votaciones_live", 0) > 0 and survey_summary.get("responses", 0) == 0:
        actions.append(
            _action(
                action_id="promote_live_vote",
                title="Impulsar votacion activa",
                description="Hay una votacion activa sin respuestas recientes. Mostrar QR/link y canales de difusion.",
                priority="medium",
                reason_code="live_vote_without_responses",
                endpoint="/api/v2/analytics/operations/dashboard",
                ui_hint="open_vote_monitor",
                href="/perfil?tab=encuestas&focus=live",
            )
        )

    if chat_available and chat_summary.get("whatsapp_messages", 0) > 0:
        actions.append(
            _action(
                action_id="monitor_whatsapp_claims",
                title="Monitorear reclamos de WhatsApp",
                description="WhatsApp tiene actividad reciente; revisar bandeja omnicanal y handoffs.",
                priority="medium",
                reason_code="whatsapp_activity",
                endpoint="/api/v2/inbox/omnichannel?channel=whatsapp",
                ui_hint="open_whatsapp_inbox",
                href=_crm_tickets_href(focus="open_whatsapp_inbox", channel="whatsapp"),
            )
        )

    if chat_available and chat_summary.get("handoff_rate", 0) >= 30:
        actions.append(
            _action(
                action_id="review_high_handoff_rate",
                title="Reducir derivaciones innecesarias",
                description="La tasa de handoff esta alta. Revisar intents, respuestas y formularios de captura.",
                priority="medium",
                reason_code="high_handoff_rate",
                endpoint="/api/v2/analytics/operations/dashboard",
                ui_hint="open_chat_quality_panel",
                href="/perfil?tab=analitica&focus=chat_quality",
            )
        )

    if commerce_available and int(commerce_summary.get("orders_needing_review") or 0) > 0:
        actions.append(
            _action(
                action_id="review_assisted_orders",
                title="Revisar pedidos asistidos",
                description="Hay notas, fotos o PDFs de pedido que la IA normalizo y esperan validacion comercial.",
                priority="high" if int(commerce_summary.get("unmatched_items") or 0) >= 3 else "medium",
                reason_code="assisted_orders_need_review",
                endpoint="/api/v2/saas/admin?module=marketplace",
                ui_hint="open_assisted_order_queue",
                href="/perfil?tab=pedidos&focus=assisted_order_queue",
            )
        )

    hotspot = (heatmap.get("hotspots") or [None])[0]
    if hotspot:
        actions.append(
            _action(
                action_id="inspect_top_hotspot",
                title="Inspeccionar hotspot principal",
                description="El mapa operativo detecto concentracion de actividad en una celda.",
                priority="medium" if hotspot.get("count", 0) < 5 else "high",
                reason_code="heatmap_hotspot",
                endpoint="/api/v2/analytics/operations/heatmap",
                payload_template={"cell_id": hotspot.get("id")},
                ui_hint="open_heatmap_cell",
                href=_crm_tickets_href(
                    focus="open_heatmap_cell",
                    category=(hotspot.get("top_categories") or [{}])[0].get("key"),
                    channel=(hotspot.get("top_channels") or [{}])[0].get("key"),
                    heatmap_cell=hotspot.get("id"),
                ),
            )
        )

    if not actions and not alerts:
        actions.append(
            _action(
                action_id="keep_monitoring",
                title="Mantener monitoreo operativo",
                description="No hay alertas criticas. Seguir observando la operacion dentro del alcance autorizado.",
                priority="low",
                reason_code="all_clear",
                endpoint="/api/v2/analytics/operations/dashboard",
                href="/perfil?tab=analitica&focus=operations",
            )
        )

    priority_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda item: priority_order.get(item.get("priority"), 9))
    return actions[:10]


def _build_ai_operational_brief(
    *,
    summary: dict[str, Any],
    alerts: list[dict[str, Any]],
    next_best_actions: list[dict[str, Any]],
    heatmap: dict[str, Any],
) -> dict[str, Any]:
    ai_summary = ((heatmap.get("ai_insights") or {}).get("summary") or {}) if isinstance(heatmap, dict) else {}
    high_alerts = [item for item in alerts if item.get("severity") == "high"]
    medium_alerts = [item for item in alerts if item.get("severity") == "medium"]
    top_action = (next_best_actions or [{}])[0] or {}
    risk_level = ai_summary.get("risk_level") or ("high" if high_alerts else "medium" if medium_alerts else "normal")
    if high_alerts or risk_level in {"critical", "high"}:
        severity = "high"
        headline = "Atencion operativa prioritaria"
    elif medium_alerts or risk_level == "medium":
        severity = "medium"
        headline = "Actividad para monitoreo cercano"
    else:
        severity = "low"
        headline = "Operacion estable"

    focus_items: list[dict[str, Any]] = []
    if summary.get("overdue_tickets", 0):
        focus_items.append(
            {
                "id": "overdue_tickets",
                "label": "Reclamos vencidos",
                "value": int(summary.get("overdue_tickets") or 0),
                "priority": "high",
                "ui_hint": "open_overdue_queue",
            }
        )
    if summary.get("heatmap_points", 0):
        focus_items.append(
            {
                "id": "heatmap_points",
                "label": "Puntos en mapa operativo",
                "value": int(summary.get("heatmap_points") or 0),
                "priority": "medium",
                "ui_hint": "open_heatmap",
            }
        )
    if summary.get("survey_responses", 0):
        focus_items.append(
            {
                "id": "survey_responses",
                "label": "Participacion en encuestas",
                "value": int(summary.get("survey_responses") or 0),
                "priority": "medium",
                "ui_hint": "open_survey_monitor",
            }
        )
    if summary.get("whatsapp_messages", 0):
        focus_items.append(
            {
                "id": "whatsapp_messages",
                "label": "Actividad WhatsApp",
                "value": int(summary.get("whatsapp_messages") or 0),
                "priority": "medium",
                "ui_hint": "open_whatsapp_inbox",
            }
        )
    if summary.get("orders_needing_review", 0):
        focus_items.append(
            {
                "id": "orders_needing_review",
                "label": "Pedidos asistidos a revisar",
                "value": int(summary.get("orders_needing_review") or 0),
                "priority": "high" if int(summary.get("unmatched_order_items") or 0) >= 3 else "medium",
                "ui_hint": "open_assisted_order_queue",
            }
        )

    if not focus_items:
        focus_items.append(
            {
                "id": "monitoring",
                "label": "Monitoreo operativo",
                "value": 0,
                "priority": "low",
                "ui_hint": "open_dashboard",
            }
        )

    dominant_intent_label = ai_summary.get("dominant_intent_label") or "consulta general"
    sentiment = ai_summary.get("sentiment") or "neutral"
    recommendation = top_action.get("title") or "Mantener monitoreo operativo"
    narrative = (
        f"{headline}: {recommendation}. "
        f"Senal dominante: {dominant_intent_label}; sentimiento: {sentiment}."
    )

    return {
        "contract_version": "operations.ai_brief.v1",
        "severity": severity,
        "headline": headline,
        "narrative": narrative,
        "risk_level": risk_level,
        "dominant_intent": ai_summary.get("dominant_intent") or "general_query",
        "dominant_intent_label": dominant_intent_label,
        "sentiment": sentiment,
        "requires_human_attention": bool(ai_summary.get("requires_human_attention") or high_alerts),
        "requires_location_focus": bool(ai_summary.get("requires_location_focus")),
        "top_action": top_action,
        "focus_items": focus_items[:5],
        "signals": {
            "alerts": len(alerts),
            "high_alerts": len(high_alerts),
            "medium_alerts": len(medium_alerts),
            "actions": len(next_best_actions or []),
            "assisted_orders": int(summary.get("assisted_orders") or 0),
            "orders_needing_review": int(summary.get("orders_needing_review") or 0),
            "hf_mode": (heatmap.get("ai_insights") or {}).get("mode"),
            "hf_configured": (((heatmap.get("ai_insights") or {}).get("hf_status") or {}).get("configured")),
        },
        "frontend_contract": {
            "render_as": "operations_ai_brief",
            "recommended_widgets": ["priority_banner", "focus_cards", "ai_signal_badges", "next_best_action"],
            "refresh_seconds": 30,
            "safe_empty_state": "show_monitoring_ok",
        },
    }


def _ai_ops_priority(item: dict[str, Any]) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(_norm(item.get("priority"), "low"), 3)


def _ai_ops_recommended_action(
    *,
    action_id: str,
    label: str,
    endpoint: str,
    ui_hint: str,
    href: str | None = None,
) -> dict[str, Any]:
    action = {
        "id": action_id,
        "label": label,
        "method": "GET",
        "endpoint": endpoint,
        "ui_hint": ui_hint,
    }
    if href:
        action["href"] = href
        action["frontend_path"] = href
    return action


def _ai_ops_ticket_items(tenant: TenantProfile, ticket_records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    tenant_slug = quote(str(getattr(tenant, "slug", "") or "").strip(), safe="")
    for record in ticket_records:
        if record.get("status") in _CLOSED_STATES:
            continue
        reason_codes: list[str] = []
        if record.get("overdue") or _norm(record.get("sla_state"), "normal") in _OVERDUE_STATES:
            reason_codes.append("sla_overdue")
        if not record.get("assignee_id"):
            reason_codes.append("unassigned")
        if _norm(record.get("priority"), "normal") in {"high", "alta", "urgent", "critica"}:
            reason_codes.append("high_priority")
        if _norm(record.get("channel"), "") in {"whatsapp", "widget"}:
            reason_codes.append("citizen_or_customer_channel")
        if not reason_codes:
            continue
        priority = "high" if "sla_overdue" in reason_codes or "high_priority" in reason_codes else "medium"
        record_id = record.get("id")
        source = _norm(record.get("source"), "ticket")
        items.append(
            {
                "id": f"ticket:{source}:{record_id}",
                "source": "ticket",
                "source_model": source,
                "record_id": record_id,
                "title": "Reclamo requiere revision humana",
                "priority": priority,
                "reason_codes": reason_codes,
                "recommended_action": _ai_ops_recommended_action(
                    action_id="open_ticket",
                    label="Abrir caso",
                    endpoint=f"/api/v2/tickets/{record_id}" if source == "tenant_ticket" else "/api/v2/inbox/omnichannel",
                    ui_hint="open_ticket_detail",
                    href=(
                        f"/t/{tenant_slug}/tickets?ticket_id={quote(str(record_id), safe='')}&source={quote(source, safe='')}"
                        if tenant_slug and record_id is not None
                        else "/perfil?tab=tickets"
                    ),
                ),
                "signals": {
                    "status": record.get("status"),
                    "category": record.get("category"),
                    "channel": record.get("channel"),
                    "sla_state": record.get("sla_state"),
                    "assignee_id": record.get("assignee_id"),
                    "confidence": "deterministic",
                },
                "pii": {"redacted": True},
            }
        )
        if len(items) >= limit:
            break
    return items


def _order_metadata(order: PedidoConversacional) -> dict[str, Any]:
    metadata = _json_object(getattr(order, "metadata_payload", None))
    if metadata:
        return metadata
    items = getattr(order, "items", None)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return _json_object(items[0].get("metadata")) or _json_object(items[0])
    return {}


def _order_draft_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    draft = _json_object(metadata.get("crm_order_draft"))
    if draft:
        return draft
    handoff = _json_object(metadata.get("crm_handoff"))
    return _json_object(handoff.get("draft_order"))


def _order_needs_operator_review(order: PedidoConversacional) -> tuple[bool, dict[str, Any]]:
    metadata = _order_metadata(order)
    draft = _order_draft_from_metadata(metadata)
    summary = _json_object(draft.get("summary")) or _json_object(metadata.get("match_summary"))
    intake = _json_object(metadata.get("intake_experience"))
    needs_review = bool(
        summary.get("needs_operator_review")
        or intake.get("needs_operator_review")
        or metadata.get("needs_operator_review")
        or int(summary.get("unmatched") or 0) > 0
    )
    return needs_review, {"metadata": metadata, "draft": draft, "summary": summary, "intake": intake}


def _conversational_order_admin_id(order: PedidoConversacional) -> str:
    return f"conversational:{order.id}"


def _ai_ops_order_items(tenant: TenantProfile, start_date: datetime, end_date: datetime, *, limit: int) -> list[dict[str, Any]]:
    orders = (
        _between(PedidoConversacional.query.filter_by(tenant_id=tenant.id), PedidoConversacional.created_at, start_date, end_date)
        .order_by(PedidoConversacional.created_at.desc())
        .limit(max(limit * 3, limit))
        .all()
    )
    items: list[dict[str, Any]] = []
    for order in orders:
        needs_review, payload = _order_needs_operator_review(order)
        if not needs_review:
            continue
        summary = payload.get("summary") or {}
        reason_codes = ["order_needs_operator_review"]
        unmatched = int(summary.get("unmatched") or 0)
        detected = int(summary.get("detected") or 0)
        if unmatched:
            reason_codes.append("unmatched_items")
        if not detected:
            reason_codes.append("low_extraction_confidence")
        priority = "high" if unmatched >= 2 or not detected else "medium"
        admin_order_id = _conversational_order_admin_id(order)
        items.append(
            {
                "id": f"order:pedido_conversacional:{order.id}",
                "source": "order",
                "source_model": "pedido_conversacional",
                "record_id": order.id,
                "title": "Pedido asistido requiere revision",
                "priority": priority,
                "reason_codes": reason_codes,
                "recommended_action": _ai_ops_recommended_action(
                    action_id="open_assisted_order",
                    label="Revisar pedido",
                    endpoint=f"/api/admin/tenants/{tenant.slug}/orders/{admin_order_id}",
                    ui_hint="open_order_detail",
                    href=f"/t/{quote(str(tenant.slug), safe='')}/pedidos/{quote(str(order.id), safe='')}",
                ),
                "signals": {
                    "state": getattr(order, "estado", None),
                    "origin": getattr(order, "origen", None),
                    "matched": summary.get("matched"),
                    "unmatched": unmatched,
                    "detected": detected,
                    "confidence": "deterministic",
                },
                "pii": {"redacted": True},
            }
        )
        if len(items) >= limit:
            break
    return items


def _ai_ops_survey_items(surveys: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    live_room = _json_object(surveys.get("live_control_room"))
    monitors = live_room.get("monitors") if isinstance(live_room.get("monitors"), list) else []
    items: list[dict[str, Any]] = []
    for monitor in monitors:
        if not isinstance(monitor, dict):
            continue
        reason_codes: list[str] = []
        responses = int(monitor.get("responses") or 0)
        geo_rate = float(monitor.get("geo_coverage_rate") or 0)
        if responses == 0:
            reason_codes.append("survey_no_responses")
        if 0 < responses < 10:
            reason_codes.append("low_participation")
        if responses and geo_rate < 50:
            reason_codes.append("low_geo_coverage")
        if not bool(monitor.get("show_live_results")):
            reason_codes.append("live_results_hidden")
        if not reason_codes and bool(monitor.get("published")):
            reason_codes.append("survey_live_monitoring")
        priority = "medium" if set(reason_codes).intersection({"survey_no_responses", "low_geo_coverage"}) else "low"
        record_id = monitor.get("id")
        items.append(
            {
                "id": f"survey:enc_encuesta:{record_id}",
                "source": "survey",
                "source_model": "enc_encuesta",
                "record_id": record_id,
                "title": "Encuesta o votacion en monitoreo",
                "priority": priority,
                "reason_codes": reason_codes,
                "recommended_action": _ai_ops_recommended_action(
                    action_id="open_survey_analytics",
                    label="Ver analitica",
                    endpoint=f"/api/v2/public/surveys/{monitor.get('public_token')}/live-results",
                    ui_hint="open_survey_analytics",
                    href=(
                        f"/admin/encuestas/{quote(str(record_id), safe='')}/analytics?focus=live"
                        if record_id is not None
                        else "/admin/encuestas"
                    ),
                ),
                "signals": {
                    "status": monitor.get("status"),
                    "responses": responses,
                    "geo_coverage_rate": geo_rate,
                    "confidence": "deterministic",
                },
                "pii": {"redacted": True},
            }
        )
        if len(items) >= limit:
            break
    return items


def build_ai_ops_queue(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    limit: int = 15,
    viewer: Any = None,
) -> dict[str, Any]:
    employee_view = is_employee_heatmap_viewer(viewer)
    ticket_records = _collect_ticket_records(tenant, start_date, end_date, viewer=viewer)
    survey_metrics = (
        _employee_unavailable_survey_metrics()
        if employee_view
        else _survey_metrics(tenant, start_date, end_date)
    )
    ticket_items = _ai_ops_ticket_items(tenant, ticket_records, limit=limit)
    order_items = (
        []
        if employee_view
        else _ai_ops_order_items(tenant, start_date, end_date, limit=limit)
    )
    survey_items = (
        [] if employee_view else _ai_ops_survey_items(survey_metrics, limit=limit)
    )
    items = sorted([*ticket_items, *order_items, *survey_items], key=_ai_ops_priority)[:limit]
    priority_counts = Counter(_norm(item.get("priority"), "low") for item in items)
    source_counts = Counter(_norm(item.get("source"), "unknown") for item in items)
    return {
        "contract_version": "operations.ai_ops_queue.v1",
        "agent_display_name": "Valeria IA-Analytics",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "scope": _operations_scope_contract(employee_view=employee_view),
        "summary": {
            "total": len(items),
            "high": int(priority_counts.get("high", 0)),
            "medium": int(priority_counts.get("medium", 0)),
            "low": int(priority_counts.get("low", 0)),
            "tickets_needing_human": int(source_counts.get("ticket", 0)),
            "orders_unmatched": int(source_counts.get("order", 0)),
            "survey_alerts": int(source_counts.get("survey", 0)),
            "advisory_only": True,
        },
        "advisory_policy": {
            "advisory_only": True,
            "mutates_operational_state": False,
            "requires_operator_confirmation": True,
            "human_decision_required_for_critical_actions": True,
            "external_ai_required": False,
        },
        "items": items,
        "signals": {
            "deterministic": True,
            "hf_gemini_advisory_layer": "optional_enrichment",
            "data_redaction": "pii_minimized",
        },
        "frontend_contract": {
            "render_as": "ai_ops_queue",
            "recommended_widgets": ["priority_queue", "source_tabs", "advisory_policy_banner"],
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_no_items_to_review",
        },
    }


def build_operational_dashboard(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    viewer: Any = None,
) -> dict[str, Any]:
    as_of = datetime.now(timezone.utc)
    employee_view = is_employee_heatmap_viewer(viewer)
    ticket_records = _collect_ticket_records(tenant, start_date, end_date, as_of=as_of, viewer=viewer)
    queue_records = _collect_open_ticket_records(tenant, as_of=as_of, viewer=viewer)
    queue_membership_quality = _queue_membership_quality(
        tenant,
        as_of=as_of,
        included_records=queue_records,
        viewer=viewer,
    )
    if employee_view:
        commerce_records, commerce_raw_count = [], 0
    else:
        commerce_records, commerce_raw_count = _collect_commerce_records(
            tenant,
            start_date,
            end_date,
        )
    ticket_metrics = _ticket_metrics(ticket_records)
    queue_ticket_metrics = _ticket_metrics(queue_records)
    ticket_metrics["grain"] = "one_ticket_created_in_period"
    ticket_metrics["period"] = {"from": _iso(start_date), "to": _iso(end_date)}
    survey_metrics = (
        _employee_unavailable_survey_metrics()
        if employee_view
        else _survey_metrics(tenant, start_date, end_date)
    )
    chat_metrics = (
        _employee_unavailable_chat_metrics()
        if employee_view
        else _chat_metrics(tenant, start_date, end_date)
    )
    commerce_metrics = (
        _employee_unavailable_commerce_metrics()
        if employee_view
        else _commerce_metrics(
            tenant,
            start_date,
            end_date,
            commerce_records=commerce_records,
            raw_record_count=commerce_raw_count,
        )
    )
    employee_metrics = (
        _employee_unavailable_employee_metrics()
        if employee_view
        else _employee_metrics(tenant, queue_records)
    )
    live_chat = _active_presence(queue_records)
    heatmap = build_operational_heatmap(
        tenant,
        start_date,
        end_date,
        viewer=viewer,
        ticket_records=ticket_records,
        max_points=500,
        include_ai=False,
        commerce_records=commerce_records,
    )
    alerts = _build_alerts(queue_ticket_metrics, survey_metrics, chat_metrics, employee_metrics, heatmap, commerce_metrics)
    summary = _summary_from_metrics(queue_ticket_metrics, survey_metrics, chat_metrics, employee_metrics, heatmap, alerts, commerce_metrics)
    queue_truth = _build_queue_truth(
        queue_records=queue_records,
        period_records=ticket_records,
        start_date=start_date,
        end_date=end_date,
        as_of=as_of,
        membership_quality=queue_membership_quality,
    )
    period = _period_delta(start_date, end_date)
    previous_start = start_date - period
    previous_end = start_date
    previous_ticket_records = _collect_ticket_records(
        tenant,
        previous_start,
        previous_end,
        as_of=as_of,
        viewer=viewer,
    )
    if employee_view:
        previous_commerce_records, previous_commerce_raw_count = [], 0
    else:
        previous_commerce_records, previous_commerce_raw_count = (
            _collect_commerce_records(
                tenant,
                previous_start,
                previous_end,
            )
        )
    previous_ticket_metrics = _ticket_metrics(previous_ticket_records)
    previous_survey_metrics = (
        _employee_unavailable_survey_metrics()
        if employee_view
        else _survey_metrics(tenant, previous_start, previous_end)
    )
    previous_chat_metrics = (
        _employee_unavailable_chat_metrics()
        if employee_view
        else _chat_metrics(tenant, previous_start, previous_end)
    )
    previous_commerce_metrics = (
        _employee_unavailable_commerce_metrics()
        if employee_view
        else _commerce_metrics(
            tenant,
            previous_start,
            previous_end,
            commerce_records=previous_commerce_records,
            raw_record_count=previous_commerce_raw_count,
        )
    )
    previous_employee_metrics = (
        _employee_unavailable_employee_metrics()
        if employee_view
        else _employee_metrics(tenant, previous_ticket_records)
    )
    previous_heatmap = build_operational_heatmap(
        tenant,
        previous_start,
        previous_end,
        viewer=viewer,
        ticket_records=previous_ticket_records,
        max_points=250,
        include_ai=False,
        commerce_records=previous_commerce_records,
    )
    previous_summary = _summary_from_metrics(
        previous_ticket_metrics,
        previous_survey_metrics,
        previous_chat_metrics,
        previous_employee_metrics,
        previous_heatmap,
        [],
        previous_commerce_metrics,
    )
    next_best_actions = _build_next_best_actions(queue_ticket_metrics, survey_metrics, chat_metrics, employee_metrics, heatmap, alerts, commerce_metrics)
    ai_brief = _build_ai_operational_brief(
        summary=summary,
        alerts=alerts,
        next_best_actions=next_best_actions,
        heatmap=heatmap,
    )

    trends = _build_trends(summary, previous_summary)
    if employee_view:
        unavailable_trend_keys = {
            "survey_responses",
            "live_votes",
            "chat_messages",
            "whatsapp_messages",
            "orders",
            "assisted_orders",
            "orders_needing_review",
        }
        trends["items"] = [
            item
            for item in trends.get("items") or []
            if item.get("key") not in unavailable_trend_keys
        ]
        trends.setdefault("unavailable", []).extend(
            {
                "key": key,
                "reason_code": _EMPLOYEE_SCOPE_UNAVAILABLE_REASON,
            }
            for key in sorted(unavailable_trend_keys)
        )
        summary["unavailable_metrics"] = sorted(unavailable_trend_keys | {"employees"})

    return {
        "contract_version": "operations.dashboard.v1",
        "tenant": _tenant_ref(tenant),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "scope": _operations_scope_contract(employee_view=employee_view),
        "summary": summary,
        "queue_truth": queue_truth,
        "previous_summary": previous_summary,
        "trends": trends,
        "tickets": ticket_metrics,
        "surveys": survey_metrics,
        "chats": chat_metrics,
        "commerce": commerce_metrics,
        "live_chat": live_chat,
        "employees": employee_metrics,
        "maps": {
            "heatmap": {
                "summary": heatmap["summary"],
                "bounds": heatmap["bounds"],
                "hotspots": heatmap.get("hotspots") or [],
                "render_contract": heatmap["render_contract"],
                "privacy": heatmap.get("privacy"),
                "ai_insights": heatmap.get("ai_insights"),
                "ai_layers": heatmap.get("ai_layers"),
                "map_experience": heatmap.get("map_experience"),
                "location_quality": heatmap.get("location_quality"),
                "geocoding": {
                    "candidate_count": ((heatmap.get("geocoding") or {}).get("candidate_count") or 0),
                    "reason_code": ((heatmap.get("geocoding") or {}).get("reason_code") or "no_pending_addresses"),
                },
            }
        },
        "alerts": alerts,
        "next_best_actions": next_best_actions,
        "ai_brief": ai_brief,
        "frontend_contract": {
            "recommended_views": (
                [
                    "ticket_board",
                    "live_chat_inbox",
                    "heatmap",
                    "action_center",
                ]
                if employee_view
                else [
                    "executive_summary",
                    "ticket_board",
                    "live_chat_inbox",
                    "commerce_assisted_orders",
                    "survey_vote_monitor",
                    "heatmap",
                    "interactive_globe",
                    "ai_risk_layers",
                    "employee_coverage",
                    "action_center",
                ]
            ),
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_contract_empty_state",
            "map_layers": (
                ["tickets"]
                if employee_view
                else ["tickets", "surveys", "analytics_events", "ai_risk", "whatsapp_activity", "survey_participation", "commerce_activity"]
            ),
            "exports": {
                "pdf": "/api/v2/analytics/operations/export.pdf",
                "ai_summary": "/api/v2/analytics/operations/executive-summary",
                "heatmap": "/api/v2/analytics/operations/heatmap",
                "freshness": "/api/v2/analytics/operations/freshness",
                "ai_brief": "/api/v2/analytics/operations/ai-brief",
            },
        },
    }


def build_action_center(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    dashboard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dashboard = dashboard or build_operational_dashboard(tenant, start_date, end_date)
    actions = dashboard.get("next_best_actions") or []
    high = len([item for item in actions if item.get("priority") == "high"])
    medium = len([item for item in actions if item.get("priority") == "medium"])
    low = len([item for item in actions if item.get("priority") == "low"])

    return {
        "contract_version": "operations.action_center.v1",
        "tenant": dashboard.get("tenant"),
        "generated_at": dashboard.get("generated_at"),
        "period": dashboard.get("period"),
        "scope": dashboard.get("scope") or {},
        "summary": {
            "total": len(actions),
            "high": high,
            "medium": medium,
            "low": low,
            "alerts": (dashboard.get("summary") or {}).get("alerts", 0),
        },
        "items": actions,
        "alerts": dashboard.get("alerts") or [],
        "ai_brief": dashboard.get("ai_brief") or {},
        "trends": dashboard.get("trends") or {},
        "frontend_contract": {
            "render_as": "action_center",
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_monitoring_ok",
        },
    }
