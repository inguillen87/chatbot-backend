"""Read-only official boundaries for the operational heatmap; no geocoding or DB writes."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
import re
import unicodedata
from typing import Any

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(__file__))


def _normalized_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def _normalized_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _scalar_text(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _valid_coordinates(lat: Any, lng: Any) -> tuple[float, float] | None:
    parsed_lat, parsed_lng = _finite_float(lat), _finite_float(lng)
    if parsed_lat is None or parsed_lng is None:
        return None
    if not (-90 <= parsed_lat <= 90 and -180 <= parsed_lng <= 180):
        return None
    return parsed_lat, parsed_lng


def _jurisdiction_bounds(value: Any) -> dict[str, float] | None:
    """Normalize the configured ``[west, south, east, north]`` envelope."""

    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    west, south, east, north = (_finite_float(item) for item in value)
    if None in {west, south, east, north}:
        return None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        return None
    return {
        "west": float(west),
        "south": float(south),
        "east": float(east),
        "north": float(north),
    }


def _safe_config_segment(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized or not re.fullmatch(r"[a-z0-9_-]+", normalized):
        return None
    return normalized


def _load_json_object(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validated_ring(value: Any) -> list[list[float]] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    ring: list[list[float]] = []
    for position in value:
        if not isinstance(position, (list, tuple)) or len(position) < 2:
            return None
        lng, lat = _finite_float(position[0]), _finite_float(position[1])
        coordinates = _valid_coordinates(lat, lng)
        if coordinates is None:
            return None
        ring.append([float(lng), float(lat)])
    if ring[0] != ring[-1]:
        return None
    signed_double_area = sum(
        start[0] * end[1] - end[0] * start[1]
        for start, end in zip(ring, ring[1:])
    )
    if abs(signed_double_area) <= 1e-14:
        return None
    return ring


def _validated_boundary_geometry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        rings = [_validated_ring(ring) for ring in coordinates]
        if rings and all(ring is not None for ring in rings):
            return {"type": "Polygon", "coordinates": rings}
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        polygons: list[list[list[list[float]]]] = []
        for polygon in coordinates:
            if not isinstance(polygon, list):
                return None
            rings = [_validated_ring(ring) for ring in polygon]
            if not rings or any(ring is None for ring in rings):
                return None
            polygons.append(rings)
        if polygons:
            return {"type": "MultiPolygon", "coordinates": polygons}
    return None


def _boundary_geometry_bounds(geometry: dict[str, Any]) -> dict[str, float] | None:
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return None
    positions: list[list[float]] = []

    def collect(value: Any) -> None:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            positions.append([float(value[0]), float(value[1])])
            return
        if isinstance(value, list):
            for item in value:
                collect(item)

    collect(coordinates)
    if not positions:
        return None
    lngs = [position[0] for position in positions]
    lats = [position[1] for position in positions]
    return {"west": min(lngs), "south": min(lats), "east": max(lngs), "north": max(lats)}


def _point_on_segment(lng: float, lat: float, start: list[float], end: list[float]) -> bool:
    x1, y1 = start
    x2, y2 = end
    cross = (lat - y1) * (x2 - x1) - (lng - x1) * (y2 - y1)
    if abs(cross) > 1e-10:
        return False
    return min(x1, x2) - 1e-10 <= lng <= max(x1, x2) + 1e-10 and min(y1, y2) - 1e-10 <= lat <= max(y1, y2) + 1e-10


def _ring_contains_position(ring: list[list[float]], lng: float, lat: float) -> bool:
    inside = False
    for index in range(len(ring) - 1):
        start, end = ring[index], ring[index + 1]
        if _point_on_segment(lng, lat, start, end):
            return True
        x1, y1 = start
        x2, y2 = end
        if (y1 > lat) != (y2 > lat):
            intersection = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lng < intersection:
                inside = not inside
    return inside


def _polygon_contains_position(polygon: list[list[list[float]]], lng: float, lat: float) -> bool:
    if not polygon or not _ring_contains_position(polygon[0], lng, lat):
        return False
    return not any(_ring_contains_position(hole, lng, lat) for hole in polygon[1:])


def _boundary_contains_position(geometry: dict[str, Any], lng: float, lat: float) -> bool:
    if geometry.get("type") == "Polygon":
        return _polygon_contains_position(geometry.get("coordinates") or [], lng, lat)
    if geometry.get("type") == "MultiPolygon":
        return any(
            _polygon_contains_position(polygon, lng, lat)
            for polygon in geometry.get("coordinates") or []
        )
    return False


def _load_verified_boundary(config_path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    boundary_config = payload.get("boundary")
    if not isinstance(boundary_config, dict):
        return None
    filename = _scalar_text(boundary_config.get("file"))
    expected_sha256 = _scalar_text(boundary_config.get("snapshot_sha256"))
    authority = boundary_config.get("authority")
    if (
        not filename
        or os.path.basename(filename) != filename
        or not filename.lower().endswith(".geojson")
        or not expected_sha256
        or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
        or not isinstance(authority, dict)
        or _normalized_value(authority.get("kind")) != "official"
        or not _scalar_text(authority.get("source_url"))
        or not _scalar_text(authority.get("publisher"))
        or not _scalar_text(authority.get("department_code"))
        or not _scalar_text(authority.get("global_id"))
        or not _scalar_text(payload.get("city"))
        or boundary_config.get("out_sr") != 4326
    ):
        return None
    boundary_path = os.path.join(os.path.dirname(config_path), filename)
    try:
        with open(boundary_path, "rb") as handle:
            raw_boundary = handle.read()
        if hashlib.sha256(raw_boundary).hexdigest() != expected_sha256.lower():
            return None
        collection = json.loads(raw_boundary.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(collection, dict):
        return None
    features = collection.get("features")
    if collection.get("type") != "FeatureCollection" or not isinstance(features, list) or len(features) != 1:
        return None
    feature = features[0]
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    if (
        _normalized_key(properties.get("departamen")) != _normalized_key(payload.get("city"))
        or _scalar_text(properties.get("codigo_dep")) != _scalar_text(authority.get("department_code"))
        or _scalar_text(properties.get("globalid")) != _scalar_text(authority.get("global_id"))
    ):
        return None
    geometry = _validated_boundary_geometry(feature.get("geometry"))
    bounds = _boundary_geometry_bounds(geometry or {})
    if geometry is None or bounds is None:
        return None
    public_feature = {
        "type": "Feature",
        "id": _scalar_text(authority.get("department_code")) or "official-boundary",
        "properties": {
            **{key: properties.get(key) for key in ("departamen", "codigo_dep", "globalid")},
            "name": _scalar_text(properties.get("departamen")) or _scalar_text(payload.get("city")),
        },
        "geometry": geometry,
    }
    public_authority = {
        **{key: authority.get(key) for key in
           ("kind", "publisher", "service", "source_url", "department_code", "global_id")},
        "source_ref": authority.get("source_url"),
        "snapshot_sha256": expected_sha256.lower(),
        "geometry_precision": boundary_config.get("geometry_precision"),
        "max_allowable_offset_degrees": boundary_config.get("max_allowable_offset_degrees"),
        "generalization_note": boundary_config.get("generalization_note"),
    }
    return {
        "geometry": geometry,
        "bounds": bounds,
        "authority": public_authority,
        "feature_collection": {
            "type": "FeatureCollection",
            "metadata": {
                "official": True,
                "synthetic": False,
                "source": authority.get("publisher"),
                "provenance": public_authority,
            },
            "features": [public_feature],
        },
    }


def _same_place(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """An envelope may borrow a boundary only for the same explicit place."""
    return all(
        _normalized_key(left.get(key))
        and _normalized_key(left.get(key)) == _normalized_key(right.get(key))
        for key in ("city", "state", "country")
    )


def resolve_tenant_jurisdiction(tenant: Any) -> dict[str, Any]:
    """Resolve an exact tenant config; never silently replace an official override.

    DATA_DIR has priority. A bounds-only legacy config can borrow the bundled
    boundary for the SAME path and city/state/country. A declared-but-invalid
    boundary or malformed override is a blocker, never a reason to fall back.
    No default tenant, city-name search, network access, or persistence is used.
    """
    slug = _safe_config_segment(getattr(tenant, "slug", None))
    municipio_id = _safe_config_segment(getattr(tenant, "municipio_id", None))
    candidates = []
    if slug:
        candidates.extend([os.path.join("tenants", slug, "geo.json"),
                           os.path.join("municipios", slug, "geo.json")])
    if municipio_id:
        candidates.append(os.path.join("municipios", municipio_id, "geo.json"))
    bundled_root = os.path.join(_REPOSITORY_ROOT, "data")
    roots = []
    configured_root = str(os.environ.get("DATA_DIR") or "").strip()
    if configured_root:
        roots.append((configured_root, "persistent_data"))
    roots.append((bundled_root, "bundled_data"))

    for root, storage in roots:
        for relative_path in dict.fromkeys(candidates):
            path = os.path.join(root, relative_path)
            if not os.path.lexists(path):
                continue
            payload = _load_json_object(path)
            boundary = _load_verified_boundary(path, payload) if payload else None
            boundary_storage = storage
            if payload and "boundary" not in payload and storage == "persistent_data":
                bundled_path = os.path.join(bundled_root, relative_path)
                bundled_payload = _load_json_object(bundled_path)
                if bundled_payload and _same_place(payload, bundled_payload):
                    boundary = _load_verified_boundary(bundled_path, bundled_payload)
                    if boundary:
                        boundary_storage = "bundled_data"
            config = payload or {}
            bounds = (boundary or {}).get("bounds") or _jurisdiction_bounds(config.get("bounds"))
            source = {"kind": "tenant_geo_config", "storage": storage,
                      "ref": relative_path.replace(os.sep, "/")}
            return {
                "contract_version": "operations.tenant_jurisdiction.v1",
                "state": "configured" if payload else "invalid_config",
                "enforced": True,
                "city": _scalar_text(config.get("city")),
                "state_name": _scalar_text(config.get("state")),
                "country": _scalar_text(config.get("country")),
                "bounds": bounds,
                "containment_method": "point_in_polygon" if boundary else "unverified",
                "containment_verified": bool(boundary),
                "boundary_authority": (boundary or {}).get("authority"),
                "boundary_geometry": (boundary or {}).get("geometry"),
                "boundary_feature_collection": (boundary or {}).get("feature_collection"),
                "boundary_source": {**source, "storage": boundary_storage} if boundary else None,
                "source": source,
                "truth_boundary": ("official_department_boundary_generalized" if boundary
                                   else "official_jurisdiction_boundary_unavailable"),
            }
    return {
        "contract_version": "operations.tenant_jurisdiction.v1",
        "state": "unconfigured", "enforced": False, "bounds": None,
        "containment_verified": False, "containment_method": "unverified",
        "boundary_authority": None, "source": None,
        "truth_boundary": "no_tenant_specific_boundary",
    }


def coordinate_jurisdiction_status(lat: Any, lng: Any, jurisdiction: dict[str, Any]) -> str:
    coordinates = _valid_coordinates(lat, lng)
    if coordinates is None:
        return "missing"
    geometry = jurisdiction.get("boundary_geometry")
    if jurisdiction.get("containment_verified") and isinstance(geometry, dict):
        return "within" if _boundary_contains_position(geometry, coordinates[1], coordinates[0]) else "outside"
    return "unverified"


def scope_heatmap_points(
    points: list[dict[str, Any]], tenant: Any, jurisdiction: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply membership BEFORE truncation/aggregation; retain no rejected locations."""
    requires_boundary = bool(jurisdiction.get("enforced")) or _normalized_value(
        getattr(tenant, "tipo", None)
    ) in {"municipio", "municipality", "municipalidad", "gobierno", "government",
          "provincia", "province", "ente_publico", "public_sector"}
    accepted = []
    rejected = Counter()
    authority = jurisdiction.get("boundary_authority") or {}
    for original in points:
        status = coordinate_jurisdiction_status(original.get("lat"), original.get("lng"), jurisdiction)
        if status == "missing" or (requires_boundary and status != "within"):
            rejected[status] += 1
            continue
        point = dict(original)
        verified = status == "within"
        evidence = {
            "contract_version": "operations.point_jurisdiction_evidence.v1",
            "containment_verified": verified,
            "coordinate_jurisdiction_status": status,
            "containment_method": "point_in_polygon" if verified else None,
            "authority_kind": authority.get("kind") if verified else None,
            "source_ref": authority.get("source_ref") if verified else None,
            "snapshot_sha256": authority.get("snapshot_sha256") if verified else None,
        }
        point.update({"coordinate_jurisdiction_status": status,
                      "containment_verified": verified,
                      "source_ref": evidence["source_ref"],
                      "snapshot_sha256": evidence["snapshot_sha256"],
                      "jurisdiction_evidence": evidence})
        accepted.append(point)
    return accepted, {
        "contract_version": "operations.heatmap.jurisdiction_review.v1",
        "status": "pending" if rejected else "empty",
        "reason_code": ("official_jurisdiction_boundary_unavailable" if requires_boundary
                        and not jurisdiction.get("containment_verified") and rejected["unverified"]
                        else "coordinates_outside_jurisdiction" if rejected["outside"] else None),
        "candidate_count": rejected["outside"],
        "outside_jurisdiction_count": rejected["outside"],
        "unverified_jurisdiction_count": rejected["unverified"],
        "invalid_coordinate_count": rejected["missing"],
        "writes_performed": False,
    }


def public_jurisdiction(jurisdiction: dict[str, Any]) -> dict[str, Any]:
    """Geometry is published once in geo_layers; no private review data is included."""
    keys = {"contract_version", "state", "enforced", "city", "state_name", "country",
            "bounds", "containment_method", "containment_verified", "boundary_authority",
            "boundary_source", "source", "truth_boundary"}
    return {key: value for key, value in jurisdiction.items() if key in keys}
