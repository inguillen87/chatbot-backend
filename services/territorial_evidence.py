from __future__ import annotations

from collections import Counter
import math
import re
import unicodedata
from typing import Any, Iterable


_LATITUDE_KEYS = {"lat", "latitude", "latitud"}
_LONGITUDE_KEYS = {"lng", "lon", "long", "longitude", "longitud"}
_ADDRESS_KEYS = {"address", "calle", "direccion", "domicilio", "formattedaddress", "ubicacion"}
_ZONE_KEYS = {"barrio", "district", "distrito", "zone", "zona"}
_UNKNOWN_ZONE_VALUES = {
    "", "desconocido", "missing", "no informado", "sin barrio",
    "sin distrito", "sin zona", "sin_zona", "unknown",
}


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


def _walk(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    budget: list[int] | None = None,
) -> Iterable[tuple[str, Any]]:
    remaining = budget if budget is not None else [1000]
    if remaining[0] <= 0 or depth > 8:
        return
    remaining[0] -= 1
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, path=f"{path}.{key}", depth=depth + 1, budget=remaining)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value[:200]):
            yield from _walk(child, path=f"{path}[{index}]", depth=depth + 1, budget=remaining)


def _entry(data: dict[str, Any], aliases: set[str]) -> tuple[str, Any] | None:
    for key, value in data.items():
        if _normalized_key(key) in aliases and value not in (None, ""):
            return str(key), value
    return None


def _provenance(source: str, path: str) -> dict[str, Any]:
    quality = {
        "ticket_columns": "persisted_ticket_columns",
        "persisted_session_context": "persisted_session_context",
    }.get(source, "persisted_structured_metadata")
    return {
        "source": source,
        "path": path,
        "quality": quality,
        "confidence": 1.0 if source == "ticket_columns" else (0.88 if source == "persisted_session_context" else 0.96),
        "requires_review": False,
    }


def extract_location_evidence(*sources: tuple[str, Any]) -> dict[str, Any]:
    """Resolve structured persisted location fields, without geocoding or writes."""

    result: dict[str, Any] = {"lat": None, "lng": None, "address": None, "zone": None}
    provenance: dict[str, Any] = {
        "contract_version": "operations.location_provenance.v1",
        "coordinate": {"status": "missing"},
        "address": {"status": "missing"},
        "zone": {"status": "missing"},
        "external_geocoding_calls": 0,
        "writes_performed": False,
    }
    for source, payload in sources:
        for path, node in _walk(payload):
            if not isinstance(node, dict):
                continue
            if result["lat"] is None:
                lat_entry = _entry(node, _LATITUDE_KEYS)
                lng_entry = _entry(node, _LONGITUDE_KEYS)
                coordinates = _valid_coordinates(
                    lat_entry[1] if lat_entry else None,
                    lng_entry[1] if lng_entry else None,
                )
                if coordinates and lat_entry and lng_entry:
                    result["lat"], result["lng"] = coordinates
                    provenance["coordinate"] = _provenance(
                        source, f"{path}.{{{lat_entry[0]},{lng_entry[0]}}}"
                    )
            for field, aliases in (("address", _ADDRESS_KEYS), ("zone", _ZONE_KEYS)):
                if result[field] is not None:
                    continue
                found = _entry(node, aliases)
                scalar = _scalar_text(found[1]) if found else None
                if scalar and found:
                    result[field] = scalar
                    provenance[field] = _provenance(source, f"{path}.{found[0]}")
    result["provenance"] = provenance
    return result


def explicit_zone(value: Any) -> str | None:
    normalized = _normalized_value(value)
    return normalized if normalized not in _UNKNOWN_ZONE_VALUES else None


def _counter_items(
    counter: Counter,
    labels: dict[str, str] | None = None,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    label_map = labels or {}
    return [
        {"key": key, "label": label_map.get(key) or key, "count": int(count)}
        for key, count in counter.most_common(limit)
    ]


def build_territorial_facets(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build privileged category/address/explicit-zone facets from ticket evidence."""

    categories: dict[str, dict[str, Any]] = {}
    addresses: dict[str, dict[str, Any]] = {}
    zones: dict[str, dict[str, Any]] = {}
    evidence_sources = {"coordinate": Counter(), "address": Counter(), "zone": Counter()}

    for record in records:
        category = _normalized_value(record.get("category")) or "sin_categoria"
        zone = explicit_zone(record.get("zone"))
        address_label = _scalar_text(record.get("address"))
        address_key = _normalized_value(address_label) if address_label else None
        mapped = _valid_coordinates(record.get("lat"), record.get("lng")) is not None
        pending = bool(address_label and not mapped)

        category_item = categories.setdefault(
            category,
            {"key": category, "count": 0, "mapped": 0, "pending": 0, "addresses": Counter(), "address_labels": {}, "zones": Counter()},
        )
        category_item["count"] += 1
        category_item["mapped"] += int(mapped)
        category_item["pending"] += int(pending)
        if address_key and address_label:
            category_item["addresses"][address_key] += 1
            category_item["address_labels"].setdefault(address_key, address_label)
        if zone:
            category_item["zones"][zone] += 1

        if address_key and address_label:
            item = addresses.setdefault(
                address_key,
                {"key": address_key, "label": address_label, "count": 0, "mapped": 0, "pending": 0, "categories": Counter(), "zones": Counter()},
            )
            item["count"] += 1
            item["mapped"] += int(mapped)
            item["pending"] += int(pending)
            item["categories"][category] += 1
            if zone:
                item["zones"][zone] += 1

        if zone:
            item = zones.setdefault(
                zone,
                {"key": zone, "count": 0, "mapped": 0, "categories": Counter()},
            )
            item["count"] += 1
            item["mapped"] += int(mapped)
            item["categories"][category] += 1

        provenance = record.get("location_provenance") or {}
        for field, present in (("coordinate", mapped), ("address", bool(address_label)), ("zone", bool(zone))):
            if present:
                source = _normalized_value((provenance.get(field) or {}).get("source")) or "missing"
                evidence_sources[field][source] += 1

    category_items = [
        {
            "key": item["key"], "label": item["key"], "count": item["count"],
            "mapped_count": item["mapped"], "pending_geocode_count": item["pending"],
            "top_addresses": _counter_items(item["addresses"], item["address_labels"], limit=8),
            "explicit_zones": _counter_items(item["zones"], limit=8),
        }
        for item in categories.values()
    ]
    address_items = [
        {
            "key": item["key"], "label": item["label"], "count": item["count"],
            "mapped_count": item["mapped"], "pending_geocode_count": item["pending"],
            "categories": _counter_items(item["categories"], limit=8),
            "explicit_zones": _counter_items(item["zones"], limit=8),
        }
        for item in addresses.values()
    ]
    zone_items = [
        {
            "key": item["key"], "label": item["key"], "count": item["count"],
            "mapped_count": item["mapped"], "categories": _counter_items(item["categories"], limit=8),
        }
        for item in zones.values()
    ]
    for items in (category_items, address_items, zone_items):
        items.sort(key=lambda item: (item["count"], item["key"]), reverse=True)

    mapped_count = sum(1 for record in records if _valid_coordinates(record.get("lat"), record.get("lng")))
    address_count = sum(1 for record in records if _scalar_text(record.get("address")))
    zone_count = sum(1 for record in records if explicit_zone(record.get("zone")))
    pending_count = sum(
        1 for record in records
        if _scalar_text(record.get("address")) and not _valid_coordinates(record.get("lat"), record.get("lng"))
    )
    located_count = sum(
        1 for record in records
        if _scalar_text(record.get("address")) or _valid_coordinates(record.get("lat"), record.get("lng"))
    )
    return {
        "contract_version": "operations.heatmap.territorial_facets.v1",
        "summary": {
            "ticket_records": len(records), "mapped_records": mapped_count,
            "records_with_address": address_count, "records_with_explicit_zone": zone_count,
            "pending_geocode_records": pending_count,
            "records_without_location": len(records) - located_count,
        },
        "categories": category_items,
        "addresses": address_items,
        "explicit_zones": zone_items,
        "provenance": {
            "coordinate_sources": _counter_items(evidence_sources["coordinate"]),
            "address_sources": _counter_items(evidence_sources["address"]),
            "zone_sources": _counter_items(evidence_sources["zone"]),
            "external_geocoding_calls": 0,
            "writes_performed": False,
        },
        "truth_boundary": {
            "zones": "explicit_persisted_fields_only",
            "address_only_records": "faceted_and_queued_but_not_plotted_without_coordinates",
        },
        "privacy": {"mode": "privileged_exact", "employee_response": "omitted_by_allowlist"},
    }
