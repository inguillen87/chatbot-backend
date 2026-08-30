from __future__ import annotations

"""Safe, tenant-scoped territorial geocoding workflow.

The operational heatmap already discovers tickets that have a persisted
address but no coordinates.  This module turns those discoveries into stable,
auditable work items.  It deliberately has no network provider and no write
path enabled by default.

Important boundaries:

* the address remains on the source ticket and is never copied to audit rows;
* a tenant-specific jurisdiction is mandatory before a proposal can be
  automatically eligible;
* provider results are validated for bounds, locality, province, country,
  partial matches and precision;
* applying coordinates requires four independent gates: non-dry-run mode,
  writes enabled, writer authority confirmed and an explicit callback;
* idempotency is scoped to tenant, source record, address, jurisdiction and an
  operation key.  Replaying the same operation never calls the provider twice.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Callable, Iterable, Protocol

from services.territorial_evidence import (
    coordinate_jurisdiction_status,
    normalize_address_key,
)


CONTRACT_VERSION = "operations.territorial_geocoding.v1"
AUTO_APPLY_LOCATION_TYPES = frozenset({"ROOFTOP", "RANGE_INTERPOLATED"})
VALID_STATES = frozenset({"pending", "needs_review", "applied", "failed"})


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_text(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").strip())
    ascii_text = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _coordinates(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    lat = _finite_float(value.get("lat"))
    lng = _finite_float(value.get("lng") if value.get("lng") is not None else value.get("lon"))
    if lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _safe_jurisdiction(value: dict[str, Any] | None) -> dict[str, Any]:
    jurisdiction = value if isinstance(value, dict) else {}
    raw_state = str(jurisdiction.get("state") or "").strip()
    bounds = jurisdiction.get("bounds")
    safe_bounds: dict[str, float] | None = None
    if isinstance(bounds, dict):
        try:
            candidate = {
                key: float(bounds[key])
                for key in ("west", "south", "east", "north")
            }
            if (
                -180 <= candidate["west"] < candidate["east"] <= 180
                and -90 <= candidate["south"] < candidate["north"] <= 90
            ):
                safe_bounds = candidate
        except (KeyError, TypeError, ValueError):
            safe_bounds = None
    elif isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        try:
            west, south, east, north = (float(item) for item in bounds)
            if -180 <= west < east <= 180 and -90 <= south < north <= 90:
                safe_bounds = {
                    "west": west,
                    "south": south,
                    "east": east,
                    "north": north,
                }
        except (TypeError, ValueError):
            safe_bounds = None
    return {
        "contract_version": str(
            jurisdiction.get("contract_version")
            or "operations.tenant_jurisdiction.v1"
        ),
        "state": (
            raw_state
            if raw_state in {"configured", "unconfigured"}
            else ("configured" if jurisdiction.get("enforced") and safe_bounds else "unconfigured")
        ),
        "enforced": bool(jurisdiction.get("enforced") and safe_bounds),
        "city": str(jurisdiction.get("city") or "").strip() or None,
        "state_name": str(
            jurisdiction.get("state_name")
            or jurisdiction.get("provincia")
            or (raw_state if raw_state not in {"configured", "unconfigured"} else "")
        ).strip()
        or None,
        "country": str(jurisdiction.get("country") or "").strip() or None,
        "locale": str(jurisdiction.get("locale") or "").strip() or None,
        "region_hint": str(jurisdiction.get("region_hint") or "").strip() or None,
        "bounds": safe_bounds,
        "source": jurisdiction.get("source") if isinstance(jurisdiction.get("source"), dict) else None,
    }


def provider_context_from_jurisdiction(jurisdiction: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit tenant bias understood by ``location_service``."""

    safe = _safe_jurisdiction(jurisdiction)
    bounds = safe.get("bounds")
    return {
        "city": safe.get("city"),
        "state": safe.get("state_name"),
        "country": safe.get("country") or "AR",
        "locale": safe.get("locale") or "es-AR",
        "region_hint": safe.get("region_hint") or "ar",
        "bounds": (
            [bounds["west"], bounds["south"], bounds["east"], bounds["north"]]
            if isinstance(bounds, dict)
            else None
        ),
    }


@dataclass(frozen=True)
class TerritorialGeocodingCandidate:
    tenant_id: int
    tenant_slug: str
    record_source: str
    record_id: str
    address: str
    normalized_address: str
    address_digest: str
    jurisdiction: dict[str, Any]
    jurisdiction_digest: str
    fingerprint: str
    category: str | None = None
    zone: str | None = None

    @property
    def provider_context(self) -> dict[str, Any]:
        return provider_context_from_jurisdiction(self.jurisdiction)

    def audit_identity(self) -> dict[str, Any]:
        """Return a durable identity without copying the source address."""

        return {
            "contract_version": CONTRACT_VERSION,
            "tenant_id": self.tenant_id,
            "tenant_slug": self.tenant_slug,
            "record_source": self.record_source,
            "record_id": self.record_id,
            "address_digest": self.address_digest,
            "jurisdiction_digest": self.jurisdiction_digest,
            "candidate_fingerprint": self.fingerprint,
            "category": self.category,
            "zone": self.zone,
        }


def build_territorial_geocoding_candidate(
    record: dict[str, Any],
    *,
    tenant_id: int,
    tenant_slug: str,
    jurisdiction: dict[str, Any] | None,
) -> TerritorialGeocodingCandidate | None:
    """Build one stable candidate from persisted ticket evidence only."""

    if not isinstance(record, dict):
        return None
    source = str(record.get("record_source") or record.get("source") or "").strip()
    record_id = str(record.get("record_id") or record.get("id") or "").strip()
    address = str(record.get("address") or "").strip()
    normalized_address = normalize_address_key(address)
    if not source or not record_id or not normalized_address:
        return None
    existing = _coordinates(
        {
            "lat": record.get("lat") if record.get("lat") is not None else record.get("latitud"),
            "lng": (
                record.get("lng")
                if record.get("lng") is not None
                else record.get("longitud")
            ),
        }
    )
    if existing is not None:
        return None

    safe_jurisdiction = _safe_jurisdiction(jurisdiction)
    address_digest = _canonical_digest({"address": normalized_address})
    jurisdiction_digest = _canonical_digest(safe_jurisdiction)
    identity = {
        "tenant_id": int(tenant_id),
        "record_source": source,
        "record_id": record_id,
        "address_digest": address_digest,
        "jurisdiction_digest": jurisdiction_digest,
    }
    return TerritorialGeocodingCandidate(
        tenant_id=int(tenant_id),
        tenant_slug=str(tenant_slug or "").strip().lower(),
        record_source=source,
        record_id=record_id,
        address=address,
        normalized_address=normalized_address,
        address_digest=address_digest,
        jurisdiction=safe_jurisdiction,
        jurisdiction_digest=jurisdiction_digest,
        fingerprint=_canonical_digest(identity),
        category=str(record.get("category") or "").strip() or None,
        zone=str(record.get("zone") or "").strip() or None,
    )


def discover_territorial_geocoding_candidates(
    records: Iterable[dict[str, Any]],
    *,
    tenant_id: int,
    tenant_slug: str,
    jurisdiction: dict[str, Any] | None,
) -> list[TerritorialGeocodingCandidate]:
    """Discover and de-duplicate address-without-coordinate candidates."""

    candidates: dict[str, TerritorialGeocodingCandidate] = {}
    for record in records:
        candidate = build_territorial_geocoding_candidate(
            record,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            jurisdiction=jurisdiction,
        )
        if candidate is not None:
            candidates.setdefault(candidate.fingerprint, candidate)
    return sorted(
        candidates.values(),
        key=lambda item: (item.record_source, item.record_id, item.fingerprint),
    )


def _google_result(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    if payload is None:
        return None, "provider_no_result"
    if isinstance(payload, list):
        if not payload:
            return None, "provider_no_result"
        return payload[0] if isinstance(payload[0], dict) else None, "provider_malformed_result"
    if not isinstance(payload, dict):
        return None, "provider_malformed_result"
    if isinstance(payload.get("results"), list):
        results = payload["results"]
        if not results:
            return None, "provider_no_result"
        return results[0] if isinstance(results[0], dict) else None, "provider_malformed_result"
    return payload, None


def _component_values(result: dict[str, Any]) -> dict[str, set[str]]:
    collected: dict[str, set[str]] = {}
    components = result.get("address_components")
    if not isinstance(components, list):
        return collected
    for component in components:
        if not isinstance(component, dict):
            continue
        values = {
            _normalized_text(component.get("long_name")),
            _normalized_text(component.get("short_name")),
        } - {""}
        for type_name in component.get("types") or []:
            key = str(type_name or "").strip()
            if key:
                collected.setdefault(key, set()).update(values)
    return collected


def _matches_expected(
    expected: str | None,
    actual: set[str],
) -> bool | None:
    if not expected:
        return None
    if not actual:
        return False
    normalized = _normalized_text(expected)
    return any(normalized == value for value in actual)


def evaluate_geocoding_result(
    candidate: TerritorialGeocodingCandidate,
    provider_payload: Any,
    *,
    provider_name: str,
) -> dict[str, Any]:
    """Validate a provider proposal without writing it to a ticket."""

    result, provider_error = _google_result(provider_payload)
    if provider_error or result is None:
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "failed",
            "reason_code": provider_error or "provider_malformed_result",
            "provider": provider_name,
            "proposal": None,
            "validation": {
                "auto_apply_eligible": False,
                "issues": [provider_error or "provider_malformed_result"],
            },
        }

    geometry = result.get("geometry") if isinstance(result.get("geometry"), dict) else {}
    coordinates = _coordinates(geometry.get("location"))
    if coordinates is None:
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "failed",
            "reason_code": "provider_coordinates_invalid",
            "provider": provider_name,
            "proposal": None,
            "validation": {
                "auto_apply_eligible": False,
                "issues": ["provider_coordinates_invalid"],
            },
        }

    lat, lng = coordinates
    jurisdiction = candidate.jurisdiction
    components = _component_values(result)
    location_type = str(geometry.get("location_type") or "").strip().upper()
    partial_match = bool(result.get("partial_match"))
    jurisdiction_state = coordinate_jurisdiction_status(lat, lng, jurisdiction)
    locality_values = set().union(
        components.get("locality", set()),
        components.get("administrative_area_level_2", set()),
        components.get("postal_town", set()),
    )
    province_values = components.get("administrative_area_level_1", set())
    country_values = components.get("country", set())
    locality_match = _matches_expected(jurisdiction.get("city"), locality_values)
    province_match = _matches_expected(jurisdiction.get("state_name"), province_values)
    country_match = _matches_expected(
        jurisdiction.get("country") or "AR",
        country_values,
    )

    issues: list[str] = []
    if not jurisdiction.get("enforced"):
        issues.append("tenant_jurisdiction_unconfigured")
    if jurisdiction_state != "within":
        issues.append(
            "coordinates_outside_tenant_bounds"
            if jurisdiction_state == "outside"
            else "tenant_bounds_not_enforced"
        )
    if partial_match:
        issues.append("provider_partial_match")
    if location_type not in AUTO_APPLY_LOCATION_TYPES:
        issues.append("provider_precision_requires_review")
    if locality_match is False:
        issues.append(
            "provider_locality_missing"
            if not locality_values
            else "provider_locality_mismatch"
        )
    if province_match is False:
        issues.append(
            "provider_province_missing"
            if not province_values
            else "provider_province_mismatch"
        )
    if country_match is False:
        issues.append(
            "provider_country_missing"
            if not country_values
            else "provider_country_mismatch"
        )

    auto_apply_eligible = not issues
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "pending" if auto_apply_eligible else "needs_review",
        "reason_code": (
            "provider_result_validated" if auto_apply_eligible else issues[0]
        ),
        "provider": provider_name,
        "proposal": {
            "lat": lat,
            "lng": lng,
            "location_type": location_type or None,
            "partial_match": partial_match,
            "place_id": str(result.get("place_id") or "").strip() or None,
        },
        "validation": {
            "auto_apply_eligible": auto_apply_eligible,
            "issues": issues,
            "jurisdiction_status": jurisdiction_state,
            "locality_match": locality_match,
            "province_match": province_match,
            "country_match": country_match,
            "location_type": location_type or None,
            "partial_match": partial_match,
        },
    }


class TerritorialGeocodingAuditStore(Protocol):
    def find_attempt(
        self,
        candidate_fingerprint: str,
        request_digest: str,
    ) -> dict[str, Any] | None: ...

    def record_attempt(
        self,
        candidate: TerritorialGeocodingCandidate,
        request_digest: str,
        outcome: dict[str, Any],
        *,
        external_call_performed: bool,
        write_performed: bool,
    ) -> None: ...


class InMemoryTerritorialGeocodingAuditStore:
    """Deterministic repository used by focal tests and dry-run tooling."""

    def __init__(self) -> None:
        self.attempts: dict[tuple[str, str], dict[str, Any]] = {}

    def find_attempt(
        self,
        candidate_fingerprint: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        stored = self.attempts.get((candidate_fingerprint, request_digest))
        return json.loads(json.dumps(stored)) if stored is not None else None

    def record_attempt(
        self,
        candidate: TerritorialGeocodingCandidate,
        request_digest: str,
        outcome: dict[str, Any],
        *,
        external_call_performed: bool,
        write_performed: bool,
    ) -> None:
        key = (candidate.fingerprint, request_digest)
        self.attempts.setdefault(key, json.loads(json.dumps(outcome)))


def _request_digest(
    candidate: TerritorialGeocodingCandidate,
    *,
    idempotency_key: str | None,
    provider_name: str,
    external_calls_enabled: bool,
    dry_run: bool,
    writes_enabled: bool,
    writer_authority_confirmed: bool,
) -> str:
    return _canonical_digest(
        {
            "candidate_fingerprint": candidate.fingerprint,
            "idempotency_key": str(idempotency_key or "default").strip(),
            "provider": provider_name,
            "external_calls_enabled": bool(external_calls_enabled),
            "dry_run": bool(dry_run),
            "writes_enabled": bool(writes_enabled),
            "writer_authority_confirmed": bool(writer_authority_confirmed),
        }
    )


def process_territorial_geocoding_candidate(
    candidate: TerritorialGeocodingCandidate,
    *,
    geocoder: Callable[[str, dict[str, Any]], Any] | None = None,
    audit_store: TerritorialGeocodingAuditStore | None = None,
    idempotency_key: str | None = None,
    provider_name: str = "google",
    external_calls_enabled: bool = False,
    dry_run: bool = True,
    writes_enabled: bool = False,
    writer_authority_confirmed: bool = False,
    apply_callback: Callable[
        [TerritorialGeocodingCandidate, dict[str, Any]], bool
    ]
    | None = None,
) -> dict[str, Any]:
    """Process one candidate under fail-closed provider and write gates."""

    request_digest = _request_digest(
        candidate,
        idempotency_key=idempotency_key,
        provider_name=provider_name,
        external_calls_enabled=external_calls_enabled,
        dry_run=dry_run,
        writes_enabled=writes_enabled,
        writer_authority_confirmed=writer_authority_confirmed,
    )
    if audit_store is not None:
        cached = audit_store.find_attempt(candidate.fingerprint, request_digest)
        if cached is not None:
            return {
                **cached,
                "idempotent_replay": True,
                "request_digest": request_digest,
            }

    external_call_performed = False
    write_performed = False
    if not external_calls_enabled:
        outcome = {
            "contract_version": CONTRACT_VERSION,
            "status": "pending",
            "reason_code": "provider_execution_disabled",
            "provider": provider_name,
            "proposal": None,
            "validation": {
                "auto_apply_eligible": False,
                "issues": ["provider_execution_disabled"],
            },
        }
    elif geocoder is None:
        outcome = {
            "contract_version": CONTRACT_VERSION,
            "status": "pending",
            "reason_code": "provider_adapter_missing",
            "provider": provider_name,
            "proposal": None,
            "validation": {
                "auto_apply_eligible": False,
                "issues": ["provider_adapter_missing"],
            },
        }
    else:
        try:
            external_call_performed = True
            payload = geocoder(candidate.address, candidate.provider_context)
            outcome = evaluate_geocoding_result(
                candidate,
                payload,
                provider_name=provider_name,
            )
        except Exception:
            outcome = {
                "contract_version": CONTRACT_VERSION,
                "status": "failed",
                "reason_code": "provider_request_failed",
                "provider": provider_name,
                "proposal": None,
                "validation": {
                    "auto_apply_eligible": False,
                    "issues": ["provider_request_failed"],
                },
            }

        eligible = bool(
            (outcome.get("validation") or {}).get("auto_apply_eligible")
            and outcome.get("proposal")
        )
        if eligible:
            if dry_run:
                outcome["status"] = "pending"
                outcome["reason_code"] = "dry_run_validated"
            elif not writes_enabled:
                outcome["status"] = "pending"
                outcome["reason_code"] = "write_gate_closed"
            elif not writer_authority_confirmed:
                outcome["status"] = "pending"
                outcome["reason_code"] = "writer_authority_unconfirmed"
            elif apply_callback is None:
                outcome["status"] = "pending"
                outcome["reason_code"] = "apply_callback_missing"
            else:
                try:
                    write_performed = bool(
                        apply_callback(candidate, dict(outcome["proposal"]))
                    )
                except Exception:
                    write_performed = False
                outcome["status"] = "applied" if write_performed else "failed"
                outcome["reason_code"] = (
                    "coordinates_applied"
                    if write_performed
                    else "coordinate_write_failed"
                )

    outcome = {
        **outcome,
        "candidate": candidate.audit_identity(),
        "request_digest": request_digest,
        "external_call_performed": external_call_performed,
        "write_performed": write_performed,
        "dry_run": bool(dry_run),
        "idempotent_replay": False,
    }
    if outcome["status"] not in VALID_STATES:
        raise ValueError("invalid territorial geocoding state")
    if audit_store is not None:
        audit_store.record_attempt(
            candidate,
            request_digest,
            outcome,
            external_call_performed=external_call_performed,
            write_performed=write_performed,
        )
    return outcome


def google_geocoding_adapter(address: str, context: dict[str, Any]) -> Any:
    """Explicit adapter; importing this module alone never calls the network."""

    from services.location_service import geocode_address

    return geocode_address(address, geo_ctx=context)


__all__ = [
    "AUTO_APPLY_LOCATION_TYPES",
    "CONTRACT_VERSION",
    "InMemoryTerritorialGeocodingAuditStore",
    "TerritorialGeocodingAuditStore",
    "TerritorialGeocodingCandidate",
    "build_territorial_geocoding_candidate",
    "discover_territorial_geocoding_candidates",
    "evaluate_geocoding_result",
    "google_geocoding_adapter",
    "process_territorial_geocoding_candidate",
    "provider_context_from_jurisdiction",
]
