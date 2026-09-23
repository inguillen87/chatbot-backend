from __future__ import annotations

"""Explicit, redacted materialization of the territorial geocoding queue."""

import hashlib
import json
import re
from typing import Any, Iterable

from models import TenantProfile
from models_territorial_geocoding import (
    TerritorialGeocodingJob,
    TerritorialGeocodingReview,
    TerritorialGeocodingSyncReceipt,
)
from services.territorial_geocoding import TerritorialGeocodingCandidate
from services.territorial_geocoding_admin import TerritorialGeocodingAdminError
from services.territorial_evidence import normalize_address_key


CONTRACT_VERSION = "operations.territorial_geocoding_sync.v1"
PREVIEW_CONTRACT_VERSION = "operations.territorial_geocoding_preview.v1"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SAFE_IDENTITY_FILTER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def _safe_label(value: Any, *, maximum: int) -> str | None:
    normalized = " ".join(str(value or "").split()).strip()
    return normalized[:maximum] or None


def _safe_candidate_identity(
    candidate: TerritorialGeocodingCandidate,
) -> dict[str, Any]:
    identity = candidate.audit_identity()
    category = _safe_label(candidate.category, maximum=96)
    zone = _safe_label(candidate.zone, maximum=120)
    address_key = candidate.normalized_address
    category_key = normalize_address_key(category)
    zone_key = normalize_address_key(zone)
    if category_key and category_key == address_key:
        category = None
    if zone_key and (
        zone_key == address_key
        or (
            any(character.isdigit() for character in str(zone or ""))
            and address_key.startswith(f"{zone_key} ")
        )
    ):
        zone = None
    return {
        "contract_version": str(identity.get("contract_version") or ""),
        "tenant_id": int(candidate.tenant_id),
        "tenant_slug": _safe_label(identity.get("tenant_slug"), maximum=80),
        "record_source": _safe_label(candidate.record_source, maximum=32),
        "record_id": _safe_label(candidate.record_id, maximum=64),
        "address_digest": candidate.address_digest,
        "jurisdiction_digest": candidate.jurisdiction_digest,
        "candidate_fingerprint": candidate.fingerprint,
        "category": category,
        "zone": zone,
    }


def _preview_filter(
    value: Any,
    *,
    maximum: int,
    identity_only: bool = False,
) -> str | None:
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized:
        return None
    if len(normalized) > maximum or (
        identity_only and not _SAFE_IDENTITY_FILTER_RE.fullmatch(normalized)
    ):
        raise TerritorialGeocodingAdminError(
            "geocoding_preview_filter_invalid",
            action_hint="use_supported_filter",
            message="El filtro de la vista previa territorial no es valido.",
        )
    return normalized


def _preview_item(candidate: TerritorialGeocodingCandidate) -> dict[str, Any]:
    identity = _safe_candidate_identity(candidate)
    source_model = identity.get("record_source")
    source_id = identity.get("record_id")
    source_ref = {
        "model": source_model,
        "id": source_id,
        "tenant_scoped": True,
    }
    return {
        "id": f"{source_model}:{source_id}",
        "ticket_id": source_id,
        "source_model": source_model,
        "category": identity.get("category"),
        "zone": identity.get("zone"),
        "state": "awaiting_materialization",
        "reason_code": "persisted_address_without_coordinates",
        "provenance": {
            "source": source_ref,
            "address_evidence": "persisted_on_source",
            "coordinate_evidence": "missing_on_source",
            "provider_evidence": "not_requested",
        },
        "actions": {
            "inspect_source": {
                "enabled": True,
                "mutates_state": False,
                "ui_hint": "open_source_ticket_read_only",
                "source_ref": source_ref,
            },
            "provider_lookup": {
                "enabled": False,
                "mutates_state": False,
                "reason_code": "preview_is_provider_free",
            },
            "review": {
                "enabled": False,
                "mutates_state": False,
                "reason_code": "materialized_proposal_required",
            },
            "coordinate_write": {
                "enabled": False,
                "mutates_state": False,
                "reason_code": "preview_is_read_only",
            },
        },
    }


def preview_territorial_geocoding_queue(
    *,
    tenant_id: int,
    candidates: Iterable[TerritorialGeocodingCandidate],
    discovered: int,
    hidden: int = 0,
    page: Any = 1,
    per_page: Any = 25,
    source_model: Any = None,
    ticket_id: Any = None,
    category: Any = None,
    zone: Any = None,
) -> dict[str, Any]:
    """Serialize live queue candidates without persistence or provider calls."""

    try:
        normalized_tenant_id = int(tenant_id)
        normalized_page = max(1, int(page))
        normalized_per_page = max(1, min(int(per_page), 100))
    except (TypeError, ValueError, OverflowError) as exc:
        raise TerritorialGeocodingAdminError(
            "geocoding_preview_pagination_invalid",
            action_hint="use_supported_pagination",
        ) from exc
    if normalized_tenant_id <= 0:
        raise TerritorialGeocodingAdminError(
            "geocoding_preview_tenant_invalid",
            status_code=403,
            action_hint="check_tenant_slug",
        )

    normalized_source = _preview_filter(
        source_model, maximum=32, identity_only=True
    )
    normalized_ticket = _preview_filter(
        ticket_id, maximum=64, identity_only=True
    )
    normalized_category = _preview_filter(category, maximum=96)
    normalized_zone = _preview_filter(zone, maximum=120)

    materialized = list(candidates)
    if any(int(candidate.tenant_id) != normalized_tenant_id for candidate in materialized):
        raise TerritorialGeocodingAdminError(
            "candidate_tenant_mismatch",
            status_code=403,
            action_hint="rebuild_tenant_candidate_set",
        )
    unique_candidates = {
        candidate.fingerprint: candidate for candidate in materialized
    }
    items = [
        _preview_item(candidate)
        for candidate in sorted(
            unique_candidates.values(),
            key=lambda item: (item.record_source, item.record_id, item.fingerprint),
        )
    ]
    if normalized_source:
        expected = normalized_source.casefold()
        items = [
            item
            for item in items
            if str(item.get("source_model") or "").casefold() == expected
        ]
    if normalized_ticket:
        expected = normalized_ticket.casefold()
        items = [
            item
            for item in items
            if str(item.get("ticket_id") or "").casefold() == expected
        ]
    if normalized_category:
        expected = normalized_category.casefold()
        items = [
            item
            for item in items
            if str(item.get("category") or "").casefold() == expected
        ]
    if normalized_zone:
        expected = normalized_zone.casefold()
        items = [
            item
            for item in items
            if str(item.get("zone") or "").casefold() == expected
        ]

    def counts(field: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items:
            label = str(item.get(field) or "unclassified")
            result[label] = result.get(label, 0) + 1
        return dict(sorted(result.items()))

    total = len(items)
    start = (normalized_page - 1) * normalized_per_page
    page_items = items[start : start + normalized_per_page]
    duplicate_hidden = max(0, len(materialized) - len(unique_candidates))

    return {
        "contract_version": PREVIEW_CONTRACT_VERSION,
        "tenant_id": normalized_tenant_id,
        "summary": {
            "discovered": max(0, int(discovered)),
            "unique": len(unique_candidates),
            "matching": total,
            "hidden": max(0, int(hidden)) + duplicate_hidden,
            "by_source_model": counts("source_model"),
            "by_category": counts("category"),
            "by_zone": counts("zone"),
        },
        "filters": {
            "source_model": normalized_source,
            "ticket_id": normalized_ticket,
            "category": normalized_category,
            "zone": normalized_zone,
        },
        "population": {
            "source_family": "tickets",
            "period": "all_available",
            "eligibility": "persisted_address_without_coordinates",
            "filters_apply_to_unique_candidates": True,
        },
        "pagination": {
            "page": normalized_page,
            "per_page": normalized_per_page,
            "total": total,
            "has_next": start + normalized_per_page < total,
        },
        "items": page_items,
        "execution": {
            "read_only": True,
            "database_write_performed": False,
            "provider_call_performed": False,
            "coordinate_write_performed": False,
        },
        "privacy": {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "candidate_fingerprint_exposed": False,
            "exact_coordinates_exposed": False,
            "tenant_scoped": True,
        },
        "frontend_contract": {
            "render_as": "territorial_geocoding_preview_queue",
            "selection_key": "selected_preview_candidate",
            "empty_state": "no_pending_geocoding_candidates_for_filters",
            "read_only": True,
        },
    }


def _pending_result(candidate: TerritorialGeocodingCandidate) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "candidate": _safe_candidate_identity(candidate),
        "materialization": {
            "reason_code": "address_without_coordinates",
            "provider_call_performed": False,
            "coordinate_write_performed": False,
        },
    }


def _is_pristine_pending(job: TerritorialGeocodingJob, reviewed: set[str]) -> bool:
    return (
        job.status == "pending"
        and int(job.attempt_count or 0) == 0
        and job.id not in reviewed
        and job.provider is None
        and job.proposed_lat is None
        and job.proposed_lng is None
    )


def _set_pending_identity(
    job: TerritorialGeocodingJob,
    candidate: TerritorialGeocodingCandidate,
) -> None:
    job.contract_version = "operations.territorial_geocoding.v1"
    job.source_model = candidate.record_source
    job.source_id = candidate.record_id
    job.candidate_fingerprint = candidate.fingerprint
    job.address_digest = candidate.address_digest
    job.jurisdiction_digest = candidate.jurisdiction_digest
    job.status = "pending"
    job.reason_code = "candidate_discovered"
    job.provider = None
    job.provider_place_id = None
    job.proposed_lat = None
    job.proposed_lng = None
    job.location_type = None
    job.partial_match = None
    job.validation_json = {
        "auto_apply_eligible": False,
        "issues": ["provider_not_requested"],
    }
    job.result_json = _pending_result(candidate)
    job.attempt_count = 0
    job.last_attempt_at = None
    job.applied_at = None


def _response(
    *,
    tenant_id: int,
    discovered: int,
    created: int,
    existing: int,
    stale: int,
    refreshed: int,
    hidden: int,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": int(tenant_id),
        "summary": {
            "discovered": max(0, int(discovered)),
            "created": max(0, int(created)),
            "existing": max(0, int(existing)),
            "stale": max(0, int(stale)),
            "refreshed": max(0, int(refreshed)),
            "hidden": max(0, int(hidden)),
        },
        "provider_call_performed": False,
        "coordinate_write_performed": False,
        "execution": {
            "provider_call_performed": False,
            "coordinate_write_performed": False,
        },
        "idempotent_replay": False,
    }


def sync_territorial_geocoding_queue(
    session: Any,
    *,
    tenant_id: int,
    actor_user_id: int,
    idempotency_key: str | None,
    candidates: Iterable[TerritorialGeocodingCandidate],
    discovered: int,
    hidden: int = 0,
) -> dict[str, Any]:
    """Materialize pending jobs without provider calls or coordinate writes."""

    raw_key = str(idempotency_key or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(raw_key):
        raise TerritorialGeocodingAdminError(
            "invalid_idempotency_key",
            status_code=400,
            action_hint="send_valid_idempotency_key",
            message="Idempotency-Key es obligatorio y debe tener entre 8 y 128 caracteres seguros.",
        )

    # Serializes materialization for a tenant on PostgreSQL.  The source data
    # remains untouched and SQLite tests simply ignore the lock clause.
    tenant_exists = (
        session.query(TenantProfile.id)
        .filter(TenantProfile.id == int(tenant_id))
        .with_for_update()
        .one_or_none()
    )
    if tenant_exists is None:
        raise TerritorialGeocodingAdminError(
            "tenant_not_found",
            status_code=404,
            action_hint="check_tenant_slug",
        )

    key_hash = _digest(
        {
            "contract_version": CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "idempotency_key": raw_key,
        }
    )
    request_digest = _digest(
        {
            "contract_version": CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "actor_user_id": int(actor_user_id),
            "scope": "all_current_address_without_coordinates",
        }
    )
    receipt = session.query(TerritorialGeocodingSyncReceipt).filter_by(
        tenant_id=int(tenant_id),
        idempotency_key_hash=key_hash,
    ).one_or_none()
    if receipt is not None:
        if receipt.request_digest != request_digest:
            raise TerritorialGeocodingAdminError(
                "idempotency_key_conflict",
                status_code=409,
                action_hint="use_new_idempotency_key",
            )
        if not receipt.completed:
            raise TerritorialGeocodingAdminError(
                "sync_in_progress",
                status_code=409,
                action_hint="retry_with_same_idempotency_key",
            )
        replay = _json_copy(receipt.response_json or {})
        replay["idempotent_replay"] = True
        return replay

    receipt = TerritorialGeocodingSyncReceipt(
        tenant_id=int(tenant_id),
        actor_user_id=int(actor_user_id),
        contract_version=CONTRACT_VERSION,
        idempotency_key_hash=key_hash,
        request_digest=request_digest,
        completed=False,
        response_json={},
    )
    session.add(receipt)
    session.flush()

    materialized = list(candidates)
    if any(int(item.tenant_id) != int(tenant_id) for item in materialized):
        raise TerritorialGeocodingAdminError(
            "candidate_tenant_mismatch",
            status_code=403,
            action_hint="rebuild_tenant_candidate_set",
        )
    unique_candidates = {item.fingerprint: item for item in materialized}
    hidden_count = max(0, int(hidden)) + max(
        0, len(materialized) - len(unique_candidates)
    )

    jobs = session.query(TerritorialGeocodingJob).filter_by(
        tenant_id=int(tenant_id)
    ).all()
    reviewed = {
        row[0]
        for row in session.query(TerritorialGeocodingReview.job_id)
        .filter(TerritorialGeocodingReview.tenant_id == int(tenant_id))
        .all()
    }
    by_fingerprint = {job.candidate_fingerprint: job for job in jobs}
    by_source: dict[tuple[str, str], list[TerritorialGeocodingJob]] = {}
    for job in jobs:
        by_source.setdefault((job.source_model, job.source_id), []).append(job)

    created = existing = stale = refreshed = 0
    for candidate in sorted(
        unique_candidates.values(),
        key=lambda item: (item.record_source, item.record_id, item.fingerprint),
    ):
        exact = by_fingerprint.get(candidate.fingerprint)
        if exact is not None:
            existing += 1
            if _is_pristine_pending(exact, reviewed):
                _set_pending_identity(exact, candidate)
            continue

        source_key = (candidate.record_source, candidate.record_id)
        previous = by_source.get(source_key) or []
        if previous:
            stale += 1
        reusable = next(
            (job for job in previous if _is_pristine_pending(job, reviewed)),
            None,
        )
        if reusable is not None:
            old_fingerprint = reusable.candidate_fingerprint
            _set_pending_identity(reusable, candidate)
            by_fingerprint.pop(old_fingerprint, None)
            by_fingerprint[candidate.fingerprint] = reusable
            existing += 1
            refreshed += 1
            continue

        job = TerritorialGeocodingJob(
            tenant_id=int(tenant_id),
            contract_version="operations.territorial_geocoding.v1",
            source_model=candidate.record_source,
            source_id=candidate.record_id,
            candidate_fingerprint=candidate.fingerprint,
            address_digest=candidate.address_digest,
            jurisdiction_digest=candidate.jurisdiction_digest,
            status="pending",
            reason_code="candidate_discovered",
            validation_json={
                "auto_apply_eligible": False,
                "issues": ["provider_not_requested"],
            },
            result_json=_pending_result(candidate),
            attempt_count=0,
        )
        session.add(job)
        session.flush()
        jobs.append(job)
        by_fingerprint[candidate.fingerprint] = job
        by_source.setdefault(source_key, []).append(job)
        created += 1

    payload = _response(
        tenant_id=int(tenant_id),
        discovered=int(discovered),
        created=created,
        existing=existing,
        stale=stale,
        refreshed=refreshed,
        hidden=hidden_count,
    )
    receipt.response_json = _json_copy(payload)
    receipt.completed = True
    session.flush()
    return payload


__all__ = [
    "CONTRACT_VERSION",
    "PREVIEW_CONTRACT_VERSION",
    "preview_territorial_geocoding_queue",
    "sync_territorial_geocoding_queue",
]
