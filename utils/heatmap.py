"""Utilities for normalizing heatmap payloads for multiple map providers."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Sequence, Tuple

try:  # pragma: no cover - optional dependency used when available
    import h3  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    h3 = None


def _init_h3_helpers() -> Tuple[Any, Any]:  # pragma: no cover - exercised indirectly
    """Return callables for converting between geo coords and H3 cells."""

    if not h3:
        return None, None

    to_cell = None
    to_geo = None

    if hasattr(h3, "geo_to_h3"):
        to_cell = h3.geo_to_h3  # type: ignore[attr-defined]
    elif hasattr(h3, "latlng_to_cell"):
        latlng_to_cell = h3.latlng_to_cell  # type: ignore[attr-defined]

        def _call(lat: float, lng: float, resolution: int) -> str:
            try:
                return latlng_to_cell(lat, lng, resolution)
            except TypeError:
                return latlng_to_cell((lat, lng), resolution)

        to_cell = _call

    if hasattr(h3, "h3_to_geo"):
        to_geo = h3.h3_to_geo  # type: ignore[attr-defined]
    elif hasattr(h3, "cell_to_latlng"):
        cell_to_latlng = h3.cell_to_latlng  # type: ignore[attr-defined]

        def _to_latlng(cell_id: str):
            result = cell_to_latlng(cell_id)
            if isinstance(result, (tuple, list)) and len(result) >= 2:
                return result[0], result[1]
            if hasattr(result, "lat") and hasattr(result, "lng"):
                return result.lat, result.lng
            if hasattr(result, "lat") and hasattr(result, "lon"):
                return result.lat, result.lon
            return None, None

        to_geo = _to_latlng

    return to_cell, to_geo


_H3_TO_CELL, _H3_CELL_TO_GEO = _init_h3_helpers()


def compute_heatmap_cell_id(lat: float, lng: float, resolution: int) -> str:
    """Return the identifier for the grid cell containing ``lat``/``lng``.

    The helper prefers the H3 library when available to obtain deterministic
    hexagonal cells.  When the optional dependency is not present we fall back
    to a simple rounded grid identifier.
    """

    if _H3_TO_CELL:
        try:
            return _H3_TO_CELL(lat, lng, resolution)
        except Exception:  # pragma: no cover - defensive fallback
            pass
    return f"grid_{round(lat, 3)}_{round(lng, 3)}_{int(resolution)}"


def compute_heatmap_centroid(
    cell_id: str,
    *,
    lat_sum: float,
    lng_sum: float,
    count: float,
) -> Tuple[Optional[float], Optional[float]]:
    """Return the centroid for an aggregated cell."""

    if _H3_CELL_TO_GEO and not cell_id.startswith("grid_"):
        try:
            lat, lng = _H3_CELL_TO_GEO(cell_id)
            if lat is not None and lng is not None:
                return float(lat), float(lng)
        except Exception:  # pragma: no cover - defensive fallback
            pass

    if not count:
        return None, None
    return lat_sum / count, lng_sum / count


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


def aggregate_heatmap_points(
    points: Sequence[Mapping[str, Any]],
    *,
    resolution: int = 8,
    lat_key: str = "lat",
    lon_key: str = "lng",
    weight_key: str = "weight",
    categorical_keys: Iterable[str] = (),
    max_category_items: int = 5,
) -> Tuple[list[dict[str, Any]], Dict[str, Any]]:
    """Aggregate normalized points into grid cells for MapLibre visualisations."""

    cells: dict[str, dict[str, Any]] = {}
    max_cell_weight = 0.0
    point_count = 0
    total_weight = 0.0
    max_point_weight = 0.0
    min_lat = None
    max_lat = None
    min_lng = None
    max_lng = None
    sum_lat = 0.0
    sum_lng = 0.0

    for point in points:
        if not isinstance(point, Mapping):
            continue

        lat = _to_float(point.get(lat_key))
        lng = _to_float(point.get(lon_key) or point.get("lon"))
        if lat is None or lng is None:
            continue

        weight = _to_float(point.get(weight_key) or point.get("w") or point.get("count"))
        if weight is None or weight <= 0:
            weight = 1.0

        point_count += 1
        total_weight += weight
        if weight > max_point_weight:
            max_point_weight = weight

        if min_lat is None or lat < min_lat:
            min_lat = lat
        if max_lat is None or lat > max_lat:
            max_lat = lat
        if min_lng is None or lng < min_lng:
            min_lng = lng
        if max_lng is None or lng > max_lng:
            max_lng = lng
        sum_lat += lat
        sum_lng += lng

        cell_id = compute_heatmap_cell_id(lat, lng, resolution)
        cell = cells.setdefault(
            cell_id,
            {
                "count": 0.0,
                "lat_sum": 0.0,
                "lng_sum": 0.0,
                "point_count": 0,
                "categorical": {},
            },
        )
        cell["count"] += weight
        cell["lat_sum"] += lat * weight
        cell["lng_sum"] += lng * weight
        cell["point_count"] += 1

        categorical_map: dict[str, defaultdict[str, float]] = cell.setdefault("categorical", {})
        for key in categorical_keys:
            raw_value = point.get(key)
            if raw_value is None:
                continue
            if isinstance(raw_value, (list, tuple, set)):
                values = raw_value
            else:
                values = (raw_value,)

            counter = categorical_map.setdefault(key, defaultdict(float))
            for value in values:
                if value is None:
                    continue
                text = str(value).strip()
                if not text:
                    continue
                counter[text] += weight

    cells_payload: list[dict[str, Any]] = []

    for cell_id, data in cells.items():
        count_value = float(data.get("count") or 0.0)
        centroid_lat, centroid_lng = compute_heatmap_centroid(
            cell_id,
            lat_sum=float(data.get("lat_sum") or 0.0),
            lng_sum=float(data.get("lng_sum") or 0.0),
            count=count_value,
        )

        top_values: dict[str, list[dict[str, Any]]] = {}
        dominant_values: dict[str, Optional[str]] = {}
        categorical = data.get("categorical", {}) or {}
        for key, counter in categorical.items():
            if not counter:
                continue
            ranked = sorted(counter.items(), key=lambda item: item[1], reverse=True)
            if max_category_items:
                ranked = ranked[:max_category_items]
            top_values[key] = [
                {"value": value, "count": round(float(count), 4)} for value, count in ranked
            ]
            dominant_values[key] = ranked[0][0] if ranked else None

        cell_payload: dict[str, Any] = {
            "cell_id": cell_id,
            "count": round(count_value, 4),
            "centroid_lat": round(centroid_lat, 6) if centroid_lat is not None else None,
            "centroid_lon": round(centroid_lng, 6) if centroid_lng is not None else None,
            "point_count": int(data.get("point_count") or 0),
        }
        if top_values:
            cell_payload["top_values"] = top_values
            cell_payload["dominant_values"] = dominant_values

        cells_payload.append(cell_payload)
        if count_value > max_cell_weight:
            max_cell_weight = count_value

    cells_payload.sort(key=lambda cell: cell.get("count", 0), reverse=True)

    enrich_heatmap_cells(
        cells_payload,
        property_keys=("point_count", "top_values", "dominant_values"),
    )

    metadata: Dict[str, Any] = {
        "resolution": resolution,
        "cell_count": len(cells_payload),
        "max_cell_count": round(max_cell_weight, 4),
        "point_count": point_count,
        "total_weight": round(total_weight, 4),
        "max_point_weight": round(max_point_weight, 4),
    }

    if point_count:
        metadata["centroid"] = [
            round(sum_lng / point_count, 6),
            round(sum_lat / point_count, 6),
        ]
        metadata["bounds"] = [
            round(min_lng or 0.0, 6),
            round(min_lat or 0.0, 6),
            round(max_lng or 0.0, 6),
            round(max_lat or 0.0, 6),
        ]

    return cells_payload, metadata


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
