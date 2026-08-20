"""Server-owned classification for real, synthetic and quarantined responses.

Public metadata is intentionally not enough to turn a response into synthetic
data.  A response is a trusted seed only when every marker written by the
server-side seeding contract agrees with the persisted survey id.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Iterator, Mapping, Optional, Tuple, TypeVar


SURVEY_DEMO_SEEDING_CONTRACT_VERSION = "surveys.demo_seeding.v1"
SURVEY_RESPONSE_PROVENANCE_CONTRACT_VERSION = "surveys.response_provenance.v1"
SURVEY_RESPONSE_ORIGIN_REAL = "real"
SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO = "synthetic_demo"
SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED = "legacy_unverified"
SURVEY_RESPONSE_ORIGINS = frozenset(
    {
        SURVEY_RESPONSE_ORIGIN_REAL,
        SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
        SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    }
)

_DEMO_BATCH_PATTERN = re.compile(
    r"^seed-(?P<survey_id>[1-9][0-9]*)-(?P<timestamp>[0-9]{9,16})"
    r"(?:-(?P<nonce>[a-f0-9]{12}))?$"
)

_ResponseT = TypeVar("_ResponseT")
SURVEY_RESPONSE_SCAN_BATCH_SIZE = 256
SURVEY_RESPONSE_SCAN_MAX_BATCH_SIZE = 2_000


def is_trusted_demo_seed_metadata(
    metadata: Any,
    *,
    survey_id: int,
) -> bool:
    """Return true only for complete, survey-bound server seed metadata.

    Convenience aliases such as ``demo`` or ``synthetic`` are deliberately
    ignored: public clients may legitimately submit those business fields and
    they must never change data provenance or deletion eligibility.
    """

    if not isinstance(metadata, Mapping):
        return False
    if metadata.get("is_demo_seed") is not True:
        return False
    if (
        metadata.get("demo_seed_contract_version")
        != SURVEY_DEMO_SEEDING_CONTRACT_VERSION
    ):
        return False

    batch_id = metadata.get("demo_batch_id")
    if not isinstance(batch_id, str):
        return False
    match = _DEMO_BATCH_PATTERN.fullmatch(batch_id)
    if match is None:
        return False
    try:
        expected_survey_id = int(survey_id)
        batch_survey_id = int(match.group("survey_id"))
    except (TypeError, ValueError, OverflowError):
        return False
    return expected_survey_id > 0 and batch_survey_id == expected_survey_id


def is_legacy_unverified_seed_metadata(
    metadata: Any,
    *,
    survey_id: int,
) -> bool:
    """Recognize historical seed-shaped metadata without trusting it.

    Older server seed batches predate the versioned marker. Public metadata was
    not reserved at that time, so these rows are quarantined instead of being
    promoted to either citizen truth or trusted synthetic data.
    """

    if not isinstance(metadata, Mapping) or metadata.get("is_demo_seed") is not True:
        return False
    if (
        metadata.get("demo_seed_contract_version")
        == SURVEY_DEMO_SEEDING_CONTRACT_VERSION
    ):
        return False
    batch_id = metadata.get("demo_batch_id")
    if not isinstance(batch_id, str):
        return False
    match = _DEMO_BATCH_PATTERN.fullmatch(batch_id)
    if match is None:
        return False
    try:
        expected_survey_id = int(survey_id)
        batch_survey_id = int(match.group("survey_id"))
    except (TypeError, ValueError, OverflowError):
        return False
    return expected_survey_id > 0 and batch_survey_id == expected_survey_id


def _response_origin(response: Any, *, survey_id: Optional[int] = None) -> str:
    """Resolve provenance conservatively for ORM rows and legacy value objects."""

    missing = object()
    persisted_origin = getattr(response, "response_origin", missing)
    if persisted_origin is not missing:
        normalized = str(persisted_origin or "").strip().lower()
        return (
            normalized
            if normalized in SURVEY_RESPONSE_ORIGINS
            else SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
        )

    resolved_survey_id = survey_id
    if resolved_survey_id is None:
        resolved_survey_id = getattr(response, "encuesta_id", None)
    try:
        resolved_survey_id = int(resolved_survey_id)
    except (TypeError, ValueError, OverflowError):
        return SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
    metadata = getattr(response, "metadata_payload", None)
    if is_trusted_demo_seed_metadata(metadata, survey_id=resolved_survey_id):
        return SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
    if is_legacy_unverified_seed_metadata(metadata, survey_id=resolved_survey_id):
        return SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
    return SURVEY_RESPONSE_ORIGIN_REAL


def is_trusted_demo_seed_response(
    response: Any,
    *,
    survey_id: Optional[int] = None,
) -> bool:
    """Classify a response using the server-owned persisted origin.

    ORM rows always expose ``response_origin`` after the provenance migration,
    so metadata can no longer promote a citizen response to synthetic. The
    metadata fallback exists only for migration/backfill compatibility and
    small value objects that predate the persisted column.
    """

    return (
        _response_origin(response, survey_id=survey_id)
        == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
    )


def is_legacy_unverified_response(
    response: Any,
    *,
    survey_id: Optional[int] = None,
) -> bool:
    """Return true for persisted or conservatively inferred quarantine rows."""

    return (
        _response_origin(response, survey_id=survey_id)
        == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
    )


def filter_survey_response_query_by_origin(
    query: Any,
    response_model: Any,
    *,
    mode: str = "real",
) -> Any:
    """Apply the indexable, server-owned provenance predicate to an ORM query."""

    normalized_mode = str(mode or "real").strip().lower()
    if normalized_mode == "real":
        expected_origin = SURVEY_RESPONSE_ORIGIN_REAL
    elif normalized_mode == "synthetic":
        expected_origin = SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
    else:
        raise ValueError("mode must be real or synthetic")
    return query.filter(response_model.response_origin == expected_origin)


def partition_survey_responses(
    responses: Iterable[_ResponseT],
    *,
    survey_id: Optional[int] = None,
) -> Tuple[list[_ResponseT], list[_ResponseT]]:
    """Partition reportable responses; quarantine rows are excluded from both."""

    real, synthetic, _unverified = partition_survey_responses_by_origin(
        responses,
        survey_id=survey_id,
    )
    return real, synthetic


def partition_survey_responses_by_origin(
    responses: Iterable[_ResponseT],
    *,
    survey_id: Optional[int] = None,
) -> Tuple[list[_ResponseT], list[_ResponseT], list[_ResponseT]]:
    """Partition all closed origins without treating quarantine as citizen data."""

    real: list[_ResponseT] = []
    synthetic: list[_ResponseT] = []
    unverified: list[_ResponseT] = []
    for response in responses:
        origin = _response_origin(response, survey_id=survey_id)
        if origin == SURVEY_RESPONSE_ORIGIN_REAL:
            target = real
        elif origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO:
            target = synthetic
        else:
            target = unverified
        target.append(response)
    return real, synthetic, unverified


def filter_real_survey_responses(
    responses: Iterable[_ResponseT],
    *,
    survey_id: Optional[int] = None,
) -> list[_ResponseT]:
    """Return only citizen/real rows; untrusted metadata remains real."""

    real, _synthetic = partition_survey_responses(
        responses,
        survey_id=survey_id,
    )
    return real


def iter_survey_response_query_bounded(
    query: Any,
    *,
    batch_size: int = SURVEY_RESPONSE_SCAN_BATCH_SIZE,
) -> Iterator[Any]:
    """Stream an ORM response query without materializing the full survey."""

    try:
        normalized_batch_size = int(batch_size)
    except (TypeError, ValueError, OverflowError):
        normalized_batch_size = SURVEY_RESPONSE_SCAN_BATCH_SIZE
    normalized_batch_size = max(
        1,
        min(normalized_batch_size, SURVEY_RESPONSE_SCAN_MAX_BATCH_SIZE),
    )
    yield from query.yield_per(normalized_batch_size)


def count_survey_responses_by_provenance(
    query: Any,
    *,
    survey_id: Optional[int] = None,
    batch_size: int = SURVEY_RESPONSE_SCAN_BATCH_SIZE,
) -> Tuple[int, int]:
    """Count real/synthetic rows with strict classification and bounded memory."""

    real_count = 0
    synthetic_count = 0
    for response in iter_survey_response_query_bounded(
        query,
        batch_size=batch_size,
    ):
        origin = _response_origin(response, survey_id=survey_id)
        if origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO:
            synthetic_count += 1
        elif origin == SURVEY_RESPONSE_ORIGIN_REAL:
            real_count += 1
    return real_count, synthetic_count


def count_survey_responses_by_origin(
    query: Any,
    *,
    survey_id: Optional[int] = None,
    batch_size: int = SURVEY_RESPONSE_SCAN_BATCH_SIZE,
) -> Tuple[int, int, int]:
    """Count every closed origin while keeping the scan memory bounded."""

    real_count = 0
    synthetic_count = 0
    unverified_count = 0
    for response in iter_survey_response_query_bounded(query, batch_size=batch_size):
        origin = _response_origin(response, survey_id=survey_id)
        if origin == SURVEY_RESPONSE_ORIGIN_REAL:
            real_count += 1
        elif origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO:
            synthetic_count += 1
        else:
            unverified_count += 1
    return real_count, synthetic_count, unverified_count


def paginate_real_survey_response_ids(
    query: Any,
    *,
    survey_id: int,
    offset: int,
    limit: int,
    batch_size: int = SURVEY_RESPONSE_SCAN_BATCH_SIZE,
) -> Tuple[list[int], int, int]:
    """Return one real-response page plus exact provenance counts.

    The caller owns the SQL ordering. Only identifiers for the requested page
    are retained; every scan batch remains bounded even for very large surveys.
    """

    normalized_offset = max(0, int(offset))
    normalized_limit = max(0, int(limit))
    page_ids: list[int] = []
    real_count = 0
    synthetic_count = 0
    for response in iter_survey_response_query_bounded(
        query,
        batch_size=batch_size,
    ):
        origin = _response_origin(response, survey_id=survey_id)
        if origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO:
            synthetic_count += 1
            continue
        if origin != SURVEY_RESPONSE_ORIGIN_REAL:
            continue
        if real_count >= normalized_offset and len(page_ids) < normalized_limit:
            response_id = getattr(response, "id", None)
            if response_id is not None:
                page_ids.append(int(response_id))
        real_count += 1
    return page_ids, real_count, synthetic_count


def build_survey_response_provenance(
    *,
    real_count: int,
    synthetic_count: int = 0,
    unverified_count: int = 0,
    mode: str = "real",
    synthetic_excluded: Optional[int] = None,
    unverified_excluded: Optional[int] = None,
) -> dict[str, Any]:
    """Build an explicit response provenance contract for API payloads."""

    normalized_mode = str(mode or "real").strip().lower()
    if normalized_mode not in {"real", "synthetic"}:
        raise ValueError("mode must be real or synthetic")
    real_total = max(0, int(real_count or 0))
    synthetic_total = max(0, int(synthetic_count or 0))
    unverified_total = max(0, int(unverified_count or 0))
    excluded_total = (
        synthetic_total
        if synthetic_excluded is None and normalized_mode == "real"
        else max(0, int(synthetic_excluded or 0))
    )
    synthetic_included = synthetic_total if normalized_mode == "synthetic" else 0
    real_included = real_total if normalized_mode == "real" else 0
    unverified_excluded_total = (
        unverified_total
        if unverified_excluded is None
        else max(0, int(unverified_excluded or 0))
    )
    return {
        "contract_version": SURVEY_RESPONSE_PROVENANCE_CONTRACT_VERSION,
        "mode": normalized_mode,
        "server_trusted_classification": True,
        "contains_synthetic": synthetic_included > 0,
        "real_responses_included": real_included,
        "synthetic_responses_included": synthetic_included,
        "synthetic_responses_excluded": excluded_total,
        "unverified_responses_included": 0,
        "unverified_responses_excluded": unverified_excluded_total,
        "synthetic_marker_contract": SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
    }
