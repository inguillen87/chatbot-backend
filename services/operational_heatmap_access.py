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
            category=record.get("category"),
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
