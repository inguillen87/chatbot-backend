from __future__ import annotations

from typing import Any

from services.employee_ticket_access import (
    employee_ticket_category_scope,
    employee_ticket_category_values_allow,
)
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role


# Institutional privacy boundary for operator-facing operational geography.
# Three decimals is roughly a neighbourhood/block centroid, while k=5 avoids
# exposing a cell backed by only one or a few people or operational records.
EMPLOYEE_HEATMAP_K_MIN = 5
EMPLOYEE_HEATMAP_COORDINATE_PRECISION = 3


def is_employee_heatmap_viewer(viewer: Any) -> bool:
    role = canonical_role(getattr(viewer, "rol", None))
    if role in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN}:
        return False
    return role == ROLE_EMPLEADO or bool(getattr(viewer, "es_empleado", False))


def employee_heatmap_scope_empty(viewer: Any) -> bool:
    if not is_employee_heatmap_viewer(viewer):
        return False
    scope = employee_ticket_category_scope(viewer)
    return not scope.names and not scope.ids


def filter_ticket_records_for_heatmap(records: list[dict[str, Any]], viewer: Any) -> list[dict[str, Any]]:
    """Enforce category scope for pre-collected records as a second boundary."""

    if not is_employee_heatmap_viewer(viewer):
        return records
    return [
        record
        for record in records
        if employee_ticket_category_values_allow(
            viewer,
            # Category authorization is defined by persisted ticket values.
            # Territorial analytics may expose an exact canonical alias for
            # grouping (for example ``alumbrado publico`` -> ``luminarias``),
            # but that presentation transform must not invalidate the
            # employee's persisted category scope at this second boundary.
            category=record.get("raw_category") or record.get("category"),
            category_id=record.get("category_id"),
        )
    ]


def heatmap_viewer_cache_signature(viewer: Any) -> tuple[Any, ...]:
    """Separate privileged and employee/category-scoped dashboard cache entries."""

    role = canonical_role(getattr(viewer, "rol", None))
    if not is_employee_heatmap_viewer(viewer):
        return (role or "anonymous", "privileged_exact")
    scope = employee_ticket_category_scope(viewer)
    return (
        role or "employee",
        getattr(viewer, "id", None),
        "employee_aggregated",
        tuple(sorted(scope.names)),
        tuple(sorted(scope.ids)),
    )


def privileged_heatmap_privacy() -> dict[str, Any]:
    return {
        "mode": "privileged_exact",
        "k_min": None,
        "coordinate_precision_decimals": None,
        "suppressed": False,
    }


def build_employee_legacy_heatmap_points(
    points: list[dict[str, Any]] | None,
    viewer: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a category-scoped, k-safe projection for legacy heatmaps.

    The legacy statistics endpoint historically returned coordinates rounded to
    four decimals, including cells backed by a single ticket.  Employee views
    must never receive those source points.  Build a new allowlisted projection
    instead: enforce the persisted employee category boundary, aggregate at
    three decimals and suppress every category/cell combination below k=5.

    ``weight`` is the number of tickets represented by a legacy source point,
    so it is the correct cardinality input after the ticket query has already
    grouped identical coordinates.
    """

    scope_empty = employee_heatmap_scope_empty(viewer)
    buckets: dict[tuple[float, float, str], dict[str, Any]] = {}
    invalid_points = 0

    if not scope_empty:
        for point in points or []:
            if not isinstance(point, dict):
                invalid_points += 1
                continue

            category = point.get("categoria")
            if category in (None, ""):
                category = point.get("category")
            category_id = point.get("categoria_id")
            if category_id is None:
                category_id = point.get("category_id")
            if not employee_ticket_category_values_allow(
                viewer,
                category=category,
                category_id=category_id,
            ):
                # Do not expose even the count of out-of-scope records.
                continue

            location = (
                point.get("location")
                if isinstance(point.get("location"), dict)
                else {}
            )
            lat_value = point.get("lat")
            lng_value = point.get("lng")
            if lat_value is None:
                lat_value = location.get("lat")
            if lng_value is None:
                lng_value = location.get("lng")
            try:
                lat = round(float(lat_value), EMPLOYEE_HEATMAP_COORDINATE_PRECISION)
                lng = round(float(lng_value), EMPLOYEE_HEATMAP_COORDINATE_PRECISION)
                weight = float(point.get("weight") or point.get("count") or 1.0)
            except (TypeError, ValueError):
                invalid_points += 1
                continue
            if weight <= 0:
                invalid_points += 1
                continue

            normalized_category = str(category or "").strip().lower()
            normalized_category_id = (
                int(category_id)
                if isinstance(category_id, int) and category_id > 0
                else None
            )
            # Persisted IDs are the canonical category identity.  Labels can
            # legitimately drift after a rename, and splitting one ID across
            # old/new labels could turn a k-safe cell (for example 2 + 3)
            # into two suppressed buckets.  Name-only legacy records retain
            # their normalized label as the fail-closed fallback identity.
            category_bucket = (
                f"id:{normalized_category_id}"
                if normalized_category_id is not None
                else f"name:{normalized_category}"
            )
            key = (lat, lng, category_bucket)
            bucket = buckets.setdefault(
                key,
                {
                    "lat": lat,
                    "lng": lng,
                    "weight": 0.0,
                    "categoria": str(category or "").strip() or None,
                    "categoria_id": normalized_category_id,
                },
            )
            bucket["weight"] = float(bucket["weight"]) + weight

    safe_points: list[dict[str, Any]] = []
    suppressed_cells = 0
    suppressed_records = 0
    for bucket in buckets.values():
        cardinality = float(bucket["weight"])
        if cardinality < EMPLOYEE_HEATMAP_K_MIN:
            suppressed_cells += 1
            suppressed_records += max(int(cardinality), 0)
            continue

        count: int | float = (
            int(cardinality) if cardinality.is_integer() else round(cardinality, 2)
        )
        safe_point: dict[str, Any] = {
            "location": {"lat": bucket["lat"], "lng": bucket["lng"]},
            "lat": bucket["lat"],
            "lng": bucket["lng"],
            "weight": count,
            "count": count,
            "categoria": bucket["categoria"],
            "privacy_mode": "employee_aggregated",
            "k_min": EMPLOYEE_HEATMAP_K_MIN,
        }
        if bucket["categoria_id"] is not None:
            safe_point["categoria_id"] = bucket["categoria_id"]
        safe_points.append(safe_point)

    safe_points.sort(
        key=lambda item: (
            float(item["weight"]),
            str(item.get("categoria") or ""),
        ),
        reverse=True,
    )
    privacy = {
        "mode": "employee_aggregated",
        "k_min": EMPLOYEE_HEATMAP_K_MIN,
        "coordinate_precision_decimals": EMPLOYEE_HEATMAP_COORDINATE_PRECISION,
        "category_scope": "empty_fail_closed" if scope_empty else "scoped",
        "suppressed": {
            "cells": suppressed_cells,
            "records": suppressed_records,
            "invalid_points": invalid_points,
            "exact_points": True,
            "low_cardinality_cells": suppressed_cells,
        },
        "empty_reason": (
            None
            if safe_points
            else ("employee_category_scope_empty" if scope_empty else "no_k_safe_cells")
        ),
    }
    return safe_points, privacy


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _safe_cells(exact_payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], int, int]:
    source_cells = (exact_payload or {}).get("cells") or []
    safe_cells: list[dict[str, Any]] = []
    suppressed_cells = 0
    suppressed_records = 0

    for cell in source_cells:
        try:
            count = int(cell.get("count") or 0)
            lat = round(float(cell.get("lat")), EMPLOYEE_HEATMAP_COORDINATE_PRECISION)
            lng = round(float(cell.get("lng")), EMPLOYEE_HEATMAP_COORDINATE_PRECISION)
            weight = round(float(cell.get("weight") or count), 2)
        except (TypeError, ValueError):
            suppressed_cells += 1
            continue
        if count < EMPLOYEE_HEATMAP_K_MIN:
            suppressed_cells += 1
            suppressed_records += max(count, 0)
            continue
        safe_cells.append({"lat": lat, "lng": lng, "count": count, "weight": weight})

    safe_cells.sort(key=lambda item: (item["weight"], item["count"]), reverse=True)
    return safe_cells, suppressed_cells, suppressed_records


def build_employee_aggregated_heatmap(
    tenant: Any,
    start_date: Any,
    end_date: Any,
    *,
    exact_payload: dict[str, Any] | None = None,
    scope_empty: bool = False,
    applied_filters: dict[str, Any] | None = None,
    bbox: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build an allowlisted employee response; never mutate/strip an exact one."""

    cells, suppressed_cells, suppressed_records = (
        ([], 0, 0)
        if scope_empty
        else _safe_cells(exact_payload)
    )
    aggregated_observations = sum(int(cell["count"]) for cell in cells)
    can_render = bool(cells)
    bounds = None
    if cells:
        lats = [cell["lat"] for cell in cells]
        lngs = [cell["lng"] for cell in cells]
        bounds = {
            "north": max(lats),
            "south": min(lats),
            "east": max(lngs),
            "west": min(lngs),
        }

    geojson_cells = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [cell["lng"], cell["lat"]],
                },
                "properties": {
                    "count": cell["count"],
                    "weight": cell["weight"],
                    "privacy_mode": "employee_aggregated",
                    "k_min": EMPLOYEE_HEATMAP_K_MIN,
                },
            }
            for cell in cells
        ],
    }

    return {
        "contract_version": "operations.heatmap.v1",
        "tenant": {
            "slug": getattr(tenant, "slug", None),
            "nombre": getattr(tenant, "nombre", None),
            "tipo": getattr(tenant, "tipo", None),
        },
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "privacy": {
            "mode": "employee_aggregated",
            "k_min": EMPLOYEE_HEATMAP_K_MIN,
            "coordinate_precision_decimals": EMPLOYEE_HEATMAP_COORDINATE_PRECISION,
            "suppressed": {
                "cells": suppressed_cells,
                "records": suppressed_records,
                "exact_points": True,
                "low_cardinality_cells": suppressed_cells,
                "record_details": True,
                "geocoding_candidates": True,
            },
            "category_scope": "empty_fail_closed" if scope_empty else "scoped",
        },
        "render_contract": {
            "state": "ready" if can_render else "empty",
            "can_render_heatmap": can_render,
            "empty_reason": (
                None
                if can_render
                else ("employee_category_scope_empty" if scope_empty else "no_k_safe_cells")
            ),
            "map_engine": "maplibre",
            "layers": ["cells"],
            "point_format": None,
            "category_layers": False,
            "address_geocoding": False,
        },
        "summary": {
            "points": aggregated_observations,
            "cells": len(cells),
            "aggregated_observations": aggregated_observations,
            "can_render_heatmap": can_render,
            "suppressed_cells": suppressed_cells,
            "suppressed_records": suppressed_records,
        },
        "applied_filters": dict(applied_filters or {}),
        "spatial_filter": {"bbox": bbox, "applied": bool(bbox)},
        "bounds": bounds,
        "cells": cells,
        "geo_layers": {
            "contract_version": "operations.heatmap_geo_layers.v1",
            "provider": "geojson",
            "coordinate_order": "lng_lat",
            "cells": geojson_cells,
        },
    }
