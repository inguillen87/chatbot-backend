from __future__ import annotations

from services.territorial_geocoding import (
    InMemoryTerritorialGeocodingAuditStore,
    build_territorial_geocoding_candidate,
    discover_territorial_geocoding_candidates,
    evaluate_geocoding_result,
    process_territorial_geocoding_candidate,
)


JUNIN_JURISDICTION = {
    "contract_version": "operations.tenant_jurisdiction.v1",
    "state": "configured",
    "enforced": True,
    "city": "Junín",
    "state_name": "Mendoza",
    "country": "AR",
    "locale": "es-AR",
    "region_hint": "ar",
    "bounds": {
        "west": -68.0,
        "south": -33.2,
        "east": -67.0,
        "north": -32.6,
    },
    "source": {"kind": "tenant_geo_config", "ref": "municipios/junin/geo.json"},
}


def _record(**overrides):
    record = {
        "source": "municipio_ticket",
        "id": 419,
        "address": "San Martín 250, Junín",
        "lat": None,
        "lng": None,
        "category": "luminarias",
        "zone": "sin_zona",
    }
    record.update(overrides)
    return record


def _candidate(jurisdiction=JUNIN_JURISDICTION):
    candidate = build_territorial_geocoding_candidate(
        _record(),
        tenant_id=7,
        tenant_slug="junin",
        jurisdiction=jurisdiction,
    )
    assert candidate is not None
    return candidate


def _provider_result(
    *,
    lat=-33.05,
    lng=-67.55,
    location_type="ROOFTOP",
    partial_match=False,
    locality="Junín",
    province="Mendoza",
    country="Argentina",
    country_short="AR",
):
    return {
        "place_id": "place-junin-419",
        "partial_match": partial_match,
        "geometry": {
            "location": {"lat": lat, "lng": lng},
            "location_type": location_type,
        },
        "address_components": [
            {
                "long_name": locality,
                "short_name": locality,
                "types": ["locality"],
            },
            {
                "long_name": province,
                "short_name": province,
                "types": ["administrative_area_level_1"],
            },
            {
                "long_name": country,
                "short_name": country_short,
                "types": ["country"],
            },
        ],
    }


def test_discovers_only_persisted_addresses_without_coordinates_and_deduplicates():
    records = [
        _record(),
        _record(),
        _record(id=420, address="", lat=None, lng=None),
        _record(id=421, lat=-33.05, lng=-67.55),
    ]

    candidates = discover_territorial_geocoding_candidates(
        records,
        tenant_id=7,
        tenant_slug="junin",
        jurisdiction=JUNIN_JURISDICTION,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.record_id == "419"
    assert candidate.normalized_address == "san martin 250 junin"
    assert candidate.provider_context["bounds"] == [-68.0, -33.2, -67.0, -32.6]
    assert candidate.audit_identity().get("address") is None
    assert len(candidate.address_digest) == 64
    assert len(candidate.fingerprint) == 64


def test_candidate_identity_changes_when_tenant_jurisdiction_changes():
    first = _candidate()
    changed = {
        **JUNIN_JURISDICTION,
        "bounds": {**JUNIN_JURISDICTION["bounds"], "east": -66.9},
    }
    second = _candidate(changed)

    assert first.address_digest == second.address_digest
    assert first.jurisdiction_digest != second.jurisdiction_digest
    assert first.fingerprint != second.fingerprint


def test_default_processing_never_calls_provider_or_writes():
    calls = {"provider": 0, "write": 0}

    def provider(address, context):
        calls["provider"] += 1
        return _provider_result()

    def writer(candidate, proposal):
        calls["write"] += 1
        return True

    result = process_territorial_geocoding_candidate(
        _candidate(),
        geocoder=provider,
        apply_callback=writer,
    )

    assert result["status"] == "pending"
    assert result["reason_code"] == "provider_execution_disabled"
    assert result["external_call_performed"] is False
    assert result["write_performed"] is False
    assert calls == {"provider": 0, "write": 0}


def test_valid_provider_result_is_dry_run_only_by_default():
    result = process_territorial_geocoding_candidate(
        _candidate(),
        geocoder=lambda address, context: _provider_result(),
        external_calls_enabled=True,
    )

    assert result["status"] == "pending"
    assert result["reason_code"] == "dry_run_validated"
    assert result["validation"]["auto_apply_eligible"] is True
    assert result["proposal"]["lat"] == -33.05
    assert result["external_call_performed"] is True
    assert result["write_performed"] is False


def test_partial_imprecise_or_outside_result_requires_review():
    candidate = _candidate()

    partial = evaluate_geocoding_result(
        candidate,
        _provider_result(partial_match=True),
        provider_name="google",
    )
    imprecise = evaluate_geocoding_result(
        candidate,
        _provider_result(location_type="APPROXIMATE"),
        provider_name="google",
    )
    outside = evaluate_geocoding_result(
        candidate,
        _provider_result(lat=-34.0, lng=-68.5),
        provider_name="google",
    )

    assert partial["status"] == "needs_review"
    assert "provider_partial_match" in partial["validation"]["issues"]
    assert imprecise["status"] == "needs_review"
    assert "provider_precision_requires_review" in imprecise["validation"]["issues"]
    assert outside["status"] == "needs_review"
    assert "coordinates_outside_tenant_bounds" in outside["validation"]["issues"]


def test_locality_province_and_country_are_validated_independently():
    result = evaluate_geocoding_result(
        _candidate(),
        _provider_result(
            locality="San Rafael",
            province="San Juan",
            country="Chile",
            country_short="CL",
        ),
        provider_name="google",
    )

    assert result["status"] == "needs_review"
    assert {
        "provider_locality_mismatch",
        "provider_province_mismatch",
        "provider_country_mismatch",
    }.issubset(result["validation"]["issues"])


def test_unconfigured_jurisdiction_can_never_be_auto_applied():
    result = evaluate_geocoding_result(
        _candidate({"state": "unconfigured", "enforced": False}),
        _provider_result(),
        provider_name="google",
    )

    assert result["status"] == "needs_review"
    assert result["validation"]["auto_apply_eligible"] is False
    assert "tenant_jurisdiction_unconfigured" in result["validation"]["issues"]


def test_full_write_gates_apply_once_and_replay_without_provider_or_write():
    audit = InMemoryTerritorialGeocodingAuditStore()
    calls = {"provider": 0, "write": 0}

    def provider(address, context):
        calls["provider"] += 1
        return _provider_result()

    def writer(candidate, proposal):
        calls["write"] += 1
        return True

    kwargs = {
        "geocoder": provider,
        "audit_store": audit,
        "idempotency_key": "junin-batch-20260830-001",
        "external_calls_enabled": True,
        "dry_run": False,
        "writes_enabled": True,
        "writer_authority_confirmed": True,
        "apply_callback": writer,
    }
    first = process_territorial_geocoding_candidate(_candidate(), **kwargs)
    replay = process_territorial_geocoding_candidate(_candidate(), **kwargs)

    assert first["status"] == "applied"
    assert first["reason_code"] == "coordinates_applied"
    assert replay["status"] == "applied"
    assert replay["idempotent_replay"] is True
    assert calls == {"provider": 1, "write": 1}


def test_provider_failures_are_audited_without_leaking_exception_text():
    def broken_provider(address, context):
        raise RuntimeError("secret provider details")

    result = process_territorial_geocoding_candidate(
        _candidate(),
        geocoder=broken_provider,
        external_calls_enabled=True,
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "provider_request_failed"
    assert "secret provider details" not in str(result)
    assert result["write_performed"] is False
