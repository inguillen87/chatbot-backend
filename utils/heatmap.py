"""Utilities for normalizing heatmap payloads for multiple map providers."""
from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return float(int(value))
        return float(value)
    except (TypeError, ValueError):
        return None


def enrich_heatmap_points(
    points: Sequence[MutableMapping[str, Any]],
    *,
    property_keys: Iterable[str] = (),
) -> Sequence[MutableMapping[str, Any]]:
    """Normalizes point entries adding GeoJSON features and Google payload fields."""

    normalized: list[MutableMapping[str, Any]] = []
    max_weight = 0.0

    for point in points:
        if not isinstance(point, MutableMapping):
            continue

        lat = _to_float(point.get("lat") or point.get("latitude"))
        lng = _to_float(point.get("lng") or point.get("lon") or point.get("longitude"))
        if (lat is None or lng is None) and isinstance(point.get("location"), Mapping):
            location = point.get("location")
            lat = lat if lat is not None else _to_float(location.get("lat"))
            lng = lng if lng is not None else _to_float(location.get("lng"))
        if lat is None or lng is None:
            continue

        point["lat"] = lat
        point["lng"] = lng

        weight_value: Any = point.get("weight")
        if weight_value is None:
            for key in ("w", "count", "total", "peso"):
                candidate = point.get(key)
                if candidate is not None:
                    weight_value = candidate
                    break
        weight = _to_float(weight_value) or 0.0
        if weight < 0:
            weight = 0.0

        point["weight"] = weight
        point["w"] = weight
        if "count" not in point:
            point["count"] = weight

        location = point.get("location")
        if isinstance(location, Mapping):
            loc_lat = _to_float(location.get("lat"))
            loc_lng = _to_float(location.get("lng"))
            if loc_lat is None or loc_lng is None:
                location = None
        if not location:
            location = {"lat": lat, "lng": lng}
        else:
            location = {"lat": _to_float(location.get("lat")) or lat, "lng": _to_float(location.get("lng")) or lng}
        point["location"] = location

        point["coordinates"] = [location["lng"], location["lat"]]

        normalized.append(point)
        if weight > max_weight:
            max_weight = weight

    if max_weight <= 0:
        max_weight = 0.0

    for point in normalized:
        weight = _to_float(point.get("weight")) or 0.0
        intensity = weight / max_weight if max_weight else 0.0
        point["intensity"] = round(intensity, 4)

        properties = {"weight": weight, "intensity": point["intensity"]}
        for key in property_keys:
            value = point.get(key)
            if value is not None:
                properties[key] = value

        point["feature"] = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [point["coordinates"][0], point["coordinates"][1]],
            },
            "properties": properties,
        }

    return points


def enrich_heatmap_cells(
    cells: Sequence[MutableMapping[str, Any]],
    *,
    lat_key: str = "centroid_lat",
    lon_key: str = "centroid_lon",
    count_key: str = "count",
    property_keys: Iterable[str] = (),
) -> Sequence[MutableMapping[str, Any]]:
    """Adds shared representations to aggregated heatmap cells."""

    normalized: list[MutableMapping[str, Any]] = []
    max_count = 0.0

    for cell in cells:
        if not isinstance(cell, MutableMapping):
            continue

        lat = _to_float(cell.get(lat_key))
        lon = _to_float(cell.get(lon_key))
        count_val = _to_float(cell.get(count_key)) or 0.0

        if lat is not None and lon is not None:
            cell[lat_key] = lat
            cell[lon_key] = lon
            cell["location"] = {"lat": lat, "lng": lon}
            cell["coordinates"] = [lon, lat]

        cell["weight"] = count_val
        if count_key not in cell or cell[count_key] is None:
            cell[count_key] = count_val

        normalized.append(cell)
        if count_val > max_count:
            max_count = count_val

    if max_count <= 0:
        max_count = 0.0

    for cell in normalized:
        weight = _to_float(cell.get("weight")) or 0.0
        intensity = weight / max_count if max_count else 0.0
        cell["intensity"] = round(intensity, 4)

        location = cell.get("location")
        if isinstance(location, Mapping):
            lat = _to_float(location.get("lat"))
            lon = _to_float(location.get("lng"))
        else:
            lat = None
            lon = None

        properties = {"weight": weight, "intensity": cell["intensity"], "cell_id": cell.get("cell_id")}
        for key in property_keys:
            value = cell.get(key)
            if value:
                properties[key] = value

        if lat is not None and lon is not None:
            cell["feature"] = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": properties,
            }

    return cells


def build_feature_collection(items: Sequence[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """Builds a GeoJSON FeatureCollection from enriched items."""

    features = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        feature = item.get("feature")
        if isinstance(feature, Mapping):
            features.append(feature)
    if not features:
        return None
    return {"type": "FeatureCollection", "features": features}


def build_google_heatmap(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Creates a list compatible with Google HeatmapLayer."""

    google_points: list[dict[str, Any]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        location = point.get("location")
        if not isinstance(location, Mapping):
            lat = _to_float(point.get("lat") or point.get("latitude"))
            lng = _to_float(point.get("lng") or point.get("lon") or point.get("longitude"))
            if lat is None or lng is None:
                continue
            location = {"lat": lat, "lng": lng}
        lat = _to_float(location.get("lat"))
        lng = _to_float(location.get("lng"))
        if lat is None or lng is None:
            continue
        weight = _to_float(point.get("weight") or point.get("w") or point.get("count"))
        if weight is None:
            continue
        google_points.append({"location": {"lat": lat, "lng": lng}, "weight": weight})
    return google_points
