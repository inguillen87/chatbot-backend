from __future__ import annotations

from collections import Counter
import json
import math
import os
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

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(__file__))


def _normalized_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def _normalized_value(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_address_key(value: Any) -> str:
    """Return a stable, non-geocoded key for an explicitly persisted address.

    The key is deliberately lexical: it does not infer a neighbourhood or call
    an external provider.  It only collapses presentation differences that
    would otherwise split the same address into separate facets.
    """

    text = _scalar_text(value)
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    ).lower()
    tokens = re.findall(r"[a-z0-9]+", ascii_text)
    aliases = {
        "av": "avenida",
        "avda": "avenida",
        "avenida": "avenida",
        "c": "calle",
        "cl": "calle",
        "calle": "calle",
    }
    normalized: list[str] = []
    for index, token in enumerate(tokens):
        # ``Nro. 123`` and ``123`` represent the same civic number.
        if token in {"n", "no", "nro", "numero"} and index + 1 < len(tokens):
            if tokens[index + 1].isdigit():
                continue
        normalized.append(aliases.get(token, token))
    return " ".join(normalized)


def normalize_address_corridor_key(value: Any) -> str:
    """Normalize the street/corridor portion without retaining house numbers."""

    text = _scalar_text(value)
    if not text:
        return ""
    street_part = re.split(r"[,;]", text, maxsplit=1)[0]
    normalized = normalize_address_key(street_part)
    return " ".join(
        token for token in normalized.split() if not any(char.isdigit() for char in token)
    )


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


def resolve_tenant_jurisdiction(tenant: Any) -> dict[str, Any]:
    """Resolve an exact tenant envelope without using a shared fallback.

    A missing tenant-specific file deliberately leaves enforcement disabled. This
    prevents the bundled Junin configuration from being applied to an unrelated
    government or company merely because it lacks its own configuration.
    """

    slug = _safe_config_segment(getattr(tenant, "slug", None))
    municipio_id = _safe_config_segment(getattr(tenant, "municipio_id", None))
    data_roots: list[tuple[str, str]] = []
    configured_data_root = str(os.environ.get("DATA_DIR") or "").strip()
    if configured_data_root:
        data_roots.append((configured_data_root, "persistent_data"))
    data_roots.append((os.path.join(_REPOSITORY_ROOT, "data"), "bundled_data"))

    relative_candidates: list[str] = []
    if slug:
        relative_candidates.extend(
            [
                os.path.join("tenants", slug, "geo.json"),
                os.path.join("municipios", slug, "geo.json"),
            ]
        )
    if municipio_id:
        relative_candidates.append(os.path.join("municipios", municipio_id, "geo.json"))

    for root, storage in data_roots:
        for relative_path in relative_candidates:
            payload = _load_json_object(os.path.join(root, relative_path))
            bounds = _jurisdiction_bounds((payload or {}).get("bounds"))
            if not payload or not bounds:
                continue
            return {
                "contract_version": "operations.tenant_jurisdiction.v1",
                "state": "configured",
                "enforced": True,
                "city": _scalar_text(payload.get("city") or payload.get("ciudad")),
                "state_name": _scalar_text(payload.get("state") or payload.get("provincia")),
                "country": _scalar_text(payload.get("country") or payload.get("pais")),
                "locale": _scalar_text(payload.get("locale")),
                "region_hint": _scalar_text(payload.get("region_hint")),
                "bounds": bounds,
                "source": {
                    "kind": "tenant_geo_config",
                    "storage": storage,
                    "ref": relative_path.replace(os.sep, "/"),
                },
                "truth_boundary": "operational_envelope_not_official_boundary",
            }

    return {
        "contract_version": "operations.tenant_jurisdiction.v1",
        "state": "unconfigured",
        "enforced": False,
        "bounds": None,
        "source": None,
        "truth_boundary": "no_tenant_specific_envelope",
    }


def coordinate_jurisdiction_status(lat: Any, lng: Any, jurisdiction: dict[str, Any] | None) -> str:
    coordinates = _valid_coordinates(lat, lng)
    if coordinates is None:
        return "missing"
    contract = jurisdiction or {}
    bounds = contract.get("bounds") if contract.get("enforced") else None
    if not isinstance(bounds, dict):
        return "unenforced"
    parsed_lat, parsed_lng = coordinates
    try:
        within = (
            float(bounds["south"]) <= parsed_lat <= float(bounds["north"])
            and float(bounds["west"]) <= parsed_lng <= float(bounds["east"])
        )
    except (KeyError, TypeError, ValueError):
        return "unenforced"
    return "within" if within else "outside"


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
        address_key = normalize_address_key(address_label) if address_label else None
        outside_jurisdiction = record.get("coordinate_jurisdiction_status") == "outside"
        mapped = (
            _valid_coordinates(record.get("lat"), record.get("lng")) is not None
            and not outside_jurisdiction
        )
        pending = bool(address_label and not mapped and not outside_jurisdiction)

        category_item = categories.setdefault(
            category,
            {"key": category, "count": 0, "mapped": 0, "pending": 0, "outside": 0, "addresses": Counter(), "address_labels": {}, "zones": Counter()},
        )
        category_item["count"] += 1
        category_item["mapped"] += int(mapped)
        category_item["pending"] += int(pending)
        category_item["outside"] += int(outside_jurisdiction)
        if address_key and address_label:
            category_item["addresses"][address_key] += 1
            category_item["address_labels"].setdefault(address_key, address_label)
        if zone:
            category_item["zones"][zone] += 1

        if address_key and address_label:
            item = addresses.setdefault(
                address_key,
                {"key": address_key, "label": address_label, "count": 0, "mapped": 0, "pending": 0, "outside": 0, "categories": Counter(), "zones": Counter()},
            )
            item["count"] += 1
            item["mapped"] += int(mapped)
            item["pending"] += int(pending)
            item["outside"] += int(outside_jurisdiction)
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
            "outside_jurisdiction_count": item["outside"],
            "top_addresses": _counter_items(item["addresses"], item["address_labels"], limit=8),
            "explicit_zones": _counter_items(item["zones"], limit=8),
        }
        for item in categories.values()
    ]
    address_items = [
        {
            "key": item["key"], "label": item["label"], "count": item["count"],
            "mapped_count": item["mapped"], "pending_geocode_count": item["pending"],
            "outside_jurisdiction_count": item["outside"],
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

    mapped_count = sum(
        1 for record in records
        if _valid_coordinates(record.get("lat"), record.get("lng"))
        and record.get("coordinate_jurisdiction_status") != "outside"
    )
    outside_count = sum(
        1 for record in records if record.get("coordinate_jurisdiction_status") == "outside"
    )
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
            "records_outside_jurisdiction": outside_count,
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
            "outside_jurisdiction": "faceted_but_excluded_from_map_pending_review",
        },
        "privacy": {"mode": "privileged_exact", "employee_response": "omitted_by_allowlist"},
    }
