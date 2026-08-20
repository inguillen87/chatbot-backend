from types import SimpleNamespace

import pytest

from services.survey_response_provenance import (
    SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
    SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    SURVEY_RESPONSE_ORIGIN_REAL,
    SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
    SURVEY_RESPONSE_PROVENANCE_CONTRACT_VERSION,
    build_survey_response_provenance,
    count_survey_responses_by_provenance,
    count_survey_responses_by_origin,
    filter_real_survey_responses,
    is_legacy_unverified_response,
    is_trusted_demo_seed_metadata,
    is_trusted_demo_seed_response,
    paginate_real_survey_response_ids,
    partition_survey_responses,
    partition_survey_responses_by_origin,
)


def _trusted_metadata(survey_id: int) -> dict:
    return {
        "is_demo_seed": True,
        "demo_seed_contract_version": SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
        "demo_batch_id": f"seed-{survey_id}-1755680400000-abcdef123456",
    }


def test_trusted_seed_requires_exact_marker_contract_and_survey_batch():
    assert is_trusted_demo_seed_metadata(_trusted_metadata(42), survey_id=42) is True

    incomplete = dict(_trusted_metadata(42))
    incomplete.pop("demo_seed_contract_version")
    assert is_trusted_demo_seed_metadata(incomplete, survey_id=42) is False

    wrong_contract = dict(_trusted_metadata(42))
    wrong_contract["demo_seed_contract_version"] = "surveys.demo_seeding.v2"
    assert is_trusted_demo_seed_metadata(wrong_contract, survey_id=42) is False

    non_boolean_marker = dict(_trusted_metadata(42))
    non_boolean_marker["is_demo_seed"] = 1
    assert is_trusted_demo_seed_metadata(non_boolean_marker, survey_id=42) is False

    assert is_trusted_demo_seed_metadata(_trusted_metadata(42), survey_id=43) is False


@pytest.mark.parametrize(
    "metadata",
    [
        {"demo": True},
        {"synthetic": True},
        {"demo": True, "synthetic": True},
        {"is_demo_seed": True},
        {
            "is_demo_seed": True,
            "demo_seed_contract_version": SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
            "demo_batch_id": "seed-7-not-a-timestamp",
        },
    ],
)
def test_public_aliases_and_incomplete_markers_are_real(metadata):
    response = SimpleNamespace(encuesta_id=7, metadata_payload=metadata)

    assert is_trusted_demo_seed_response(response) is False
    assert filter_real_survey_responses([response]) == [response]


def test_partition_and_provenance_keep_synthetic_data_explicit_and_separate():
    real = SimpleNamespace(encuesta_id=9, metadata_payload={"synthetic": True})
    synthetic = SimpleNamespace(encuesta_id=9, metadata_payload=_trusted_metadata(9))

    real_rows, synthetic_rows = partition_survey_responses([real, synthetic])
    provenance = build_survey_response_provenance(
        real_count=len(real_rows),
        synthetic_count=len(synthetic_rows),
        mode="synthetic",
    )

    assert real_rows == [real]
    assert synthetic_rows == [synthetic]
    assert provenance == {
        "contract_version": SURVEY_RESPONSE_PROVENANCE_CONTRACT_VERSION,
        "mode": "synthetic",
        "server_trusted_classification": True,
        "contains_synthetic": True,
        "real_responses_included": 0,
        "synthetic_responses_included": 1,
        "synthetic_responses_excluded": 0,
        "unverified_responses_included": 0,
        "unverified_responses_excluded": 0,
        "synthetic_marker_contract": SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
    }

    with pytest.raises(ValueError):
        build_survey_response_provenance(
            real_count=1,
            synthetic_count=1,
            mode="mixed",
        )


def test_persisted_origin_is_authoritative_over_trusted_looking_metadata():
    smuggled_real = SimpleNamespace(
        encuesta_id=9,
        response_origin=SURVEY_RESPONSE_ORIGIN_REAL,
        metadata_payload=_trusted_metadata(9),
    )
    backend_seed = SimpleNamespace(
        encuesta_id=9,
        response_origin=SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
        metadata_payload={"source": "server"},
    )
    legacy_without_origin = SimpleNamespace(
        encuesta_id=9,
        metadata_payload=_trusted_metadata(9),
    )
    quarantined = SimpleNamespace(
        encuesta_id=9,
        response_origin=SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
        metadata_payload={"source": "migration"},
    )

    assert is_trusted_demo_seed_response(smuggled_real) is False
    assert is_trusted_demo_seed_response(backend_seed) is True
    assert is_trusted_demo_seed_response(legacy_without_origin) is True
    assert is_legacy_unverified_response(quarantined) is True
    assert filter_real_survey_responses([quarantined]) == []


def test_survey_bound_legacy_marker_is_quarantined_not_real_or_synthetic():
    legacy = SimpleNamespace(
        id=7,
        encuesta_id=9,
        metadata_payload={
            "is_demo_seed": True,
            "demo_batch_id": "seed-9-1740823440",
        },
    )
    real, synthetic, unverified = partition_survey_responses_by_origin([legacy])
    assert real == []
    assert synthetic == []
    assert unverified == [legacy]
    assert partition_survey_responses([legacy]) == ([], [])


def test_bounded_query_scan_counts_and_paginates_without_materializing_all_rows():
    rows = [
        SimpleNamespace(id=1, encuesta_id=9, metadata_payload={"source": "real"}),
        SimpleNamespace(id=2, encuesta_id=9, metadata_payload=_trusted_metadata(9)),
        SimpleNamespace(id=3, encuesta_id=9, metadata_payload={"source": "real"}),
        SimpleNamespace(id=4, encuesta_id=9, metadata_payload={"source": "real"}),
        SimpleNamespace(id=5, encuesta_id=9, metadata_payload=_trusted_metadata(9)),
        SimpleNamespace(id=6, encuesta_id=9, metadata_payload={"source": "real"}),
        SimpleNamespace(
            id=7,
            encuesta_id=9,
            metadata_payload={
                "is_demo_seed": True,
                "demo_batch_id": "seed-9-1740823440",
            },
        ),
    ]

    class BoundedQuery:
        def __init__(self, values):
            self.values = values
            self.batch_sizes = []

        def yield_per(self, batch_size):
            self.batch_sizes.append(batch_size)
            return iter(self.values)

        def all(self):  # pragma: no cover - this is a regression tripwire
            raise AssertionError("bounded provenance scans must not call all()")

    page_query = BoundedQuery(rows)
    page_ids, real_count, synthetic_count = paginate_real_survey_response_ids(
        page_query,
        survey_id=9,
        offset=1,
        limit=2,
        batch_size=99_999,
    )
    assert page_ids == [3, 4]
    assert real_count == 4
    assert synthetic_count == 2
    assert page_query.batch_sizes == [2_000]

    count_query = BoundedQuery(rows)
    assert count_survey_responses_by_provenance(
        count_query,
        survey_id=9,
        batch_size=7,
    ) == (4, 2)
    assert count_query.batch_sizes == [7]

    all_origins_query = BoundedQuery(rows)
    assert count_survey_responses_by_origin(
        all_origins_query,
        survey_id=9,
        batch_size=3,
    ) == (4, 2, 1)
