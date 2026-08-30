from __future__ import annotations

"""Admin read/review boundary for the territorial geocoding queue.

The service deliberately does not import a geocoding provider or the ticket
coordinate writer.  GET operations are read-only and human reviews only append
an immutable decision receipt.  Applying coordinates remains a separate,
explicitly fenced workflow.
"""

from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError

from models_territorial_geocoding import (
    TerritorialGeocodingAttempt,
    TerritorialGeocodingJob,
    TerritorialGeocodingReview,
)


CONTRACT_VERSION = "operations.territorial_geocoding_admin.v1"
QUEUE_PATH = "/api/v2/analytics/operations/geocoding-queue"
VALID_REVIEW_STATES = frozenset(
    {"unreviewed", "approved", "rejected", "stale"}
)
APPROVAL_REASON_CODES = frozenset(
    {
        "verified_against_source",
        "verified_on_map",
        "verified_with_field_team",
    }
)
REJECTION_REASON_CODES = frozenset(
    {
        "ambiguous_candidate",
        "duplicate_job",
        "incorrect_location",
        "insufficient_precision",
        "outside_jurisdiction",
        "stale_source",
    }
)
REVIEW_REASON_CODES = {
    "approved": APPROVAL_REASON_CODES,
    "rejected": REJECTION_REASON_CODES,
}
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SAFE_FILTER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


class TerritorialGeocodingAdminError(ValueError):
    def __init__(
        self,
        reason_code: str,
        *,
        status_code: int = 400,
        action_hint: str = "check_request",
        message: str | None = None,
    ) -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.status_code = int(status_code)
        self.action_hint = action_hint
        self.message = message or reason_code


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_label(value: Any, *, maximum: int = 120) -> str | None:
    normalized = " ".join(str(value or "").split()).strip()
    return normalized[:maximum] or None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _candidate_metadata(job: TerritorialGeocodingJob) -> dict[str, Any]:
    candidate = _mapping(_mapping(job.result_json).get("candidate"))
    return {
        "category": _safe_label(candidate.get("category"), maximum=96),
        "zone": _safe_label(candidate.get("zone"), maximum=120),
    }


def _proposal_snapshot(job: TerritorialGeocodingJob) -> dict[str, Any]:
    validation = _mapping(job.validation_json)
    lat = _finite_float(job.proposed_lat)
    lng = _finite_float(job.proposed_lng)
    return {
        "lat": lat,
        "lng": lng,
        "location_type": _safe_label(job.location_type, maximum=32),
        "partial_match": (
            bool(job.partial_match) if job.partial_match is not None else None
        ),
        "provider": _safe_label(job.provider, maximum=32),
        "provider_place_id": _safe_label(job.provider_place_id, maximum=255),
        "validation": {
            "auto_apply_eligible": bool(validation.get("auto_apply_eligible")),
            "issues": [
                _safe_label(issue, maximum=96)
                for issue in (validation.get("issues") or [])
                if _safe_label(issue, maximum=96)
            ][:20],
            "jurisdiction_status": _safe_label(
                validation.get("jurisdiction_status"), maximum=32
            ),
            "locality_match": validation.get("locality_match"),
            "province_match": validation.get("province_match"),
            "country_match": validation.get("country_match"),
        },
    }


def proposal_digest(job: TerritorialGeocodingJob) -> str:
    """Bind a review to the exact proposal and validation visible to the admin."""

    return _canonical_digest(_proposal_snapshot(job))


def _quality(job: TerritorialGeocodingJob) -> dict[str, Any]:
    proposal = _proposal_snapshot(job)
    validation = proposal["validation"]
    has_proposal = proposal["lat"] is not None and proposal["lng"] is not None
    if job.status == "applied":
        state = "applied"
    elif has_proposal and validation["auto_apply_eligible"]:
        state = "validated"
    elif has_proposal:
        state = "requires_human_review"
    elif job.status == "failed":
        state = "failed_without_proposal"
    else:
        state = "awaiting_proposal"
    return {
        "state": state,
        "has_proposal": has_proposal,
        "auto_apply_eligible": bool(validation["auto_apply_eligible"]),
        "jurisdiction_status": validation["jurisdiction_status"],
        "location_type": proposal["location_type"],
        "partial_match": proposal["partial_match"],
        "locality_match": validation["locality_match"],
        "province_match": validation["province_match"],
        "country_match": validation["country_match"],
        "issues": validation["issues"],
    }


def _latest_reviews(
    session: Any,
    *,
    tenant_id: int,
    job_ids: Iterable[str],
) -> dict[str, TerritorialGeocodingReview]:
    normalized_ids = [str(job_id) for job_id in job_ids if str(job_id)]
    if not normalized_ids:
        return {}
    reviews = (
        session.query(TerritorialGeocodingReview)
        .filter(
            TerritorialGeocodingReview.tenant_id == int(tenant_id),
            TerritorialGeocodingReview.job_id.in_(normalized_ids),
        )
        .order_by(
            TerritorialGeocodingReview.created_at.asc(),
            TerritorialGeocodingReview.id.asc(),
        )
        .all()
    )
    return {review.job_id: review for review in reviews}


def _review_state(
    job: TerritorialGeocodingJob,
    review: TerritorialGeocodingReview | None,
) -> str:
    if review is None:
        return "unreviewed"
    if review.proposal_digest != proposal_digest(job):
        return "stale"
    return str(review.decision)


def _review_view(
    review: TerritorialGeocodingReview,
    *,
    current_proposal_digest: str,
) -> dict[str, Any]:
    is_current = review.proposal_digest == current_proposal_digest
    return {
        "id": review.id,
        "decision": review.decision,
        "effective_state": review.decision if is_current else "stale",
        "reason_code": review.reason_code,
        "reviewer_user_id": review.reviewer_user_id,
        "reviewed_job_status": review.reviewed_job_status,
        "proposal_current": is_current,
        "coordinate_write_performed": False,
        "created_at": _iso(review.created_at),
    }


def _actions(job: TerritorialGeocodingJob) -> dict[str, Any]:
    base = f"{QUEUE_PATH}/{job.id}"
    can_review = job.status != "applied"
    return {
        "detail": {"method": "GET", "href": base},
        "attempts": {"method": "GET", "href": f"{base}/attempts"},
        "review": {
            "method": "POST",
            "href": f"{base}/review",
            "idempotency_header": "Idempotency-Key",
            "can_approve": bool(can_review and _quality(job)["has_proposal"]),
            "can_reject": bool(can_review),
            "allowed_reason_codes": {
                decision: sorted(reason_codes)
                for decision, reason_codes in REVIEW_REASON_CODES.items()
            },
            "coordinate_application_supported": False,
        },
    }


def _item_view(
    job: TerritorialGeocodingJob,
    latest_review: TerritorialGeocodingReview | None,
) -> dict[str, Any]:
    metadata = _candidate_metadata(job)
    digest = proposal_digest(job)
    review_state = _review_state(job, latest_review)
    return {
        "id": job.id,
        "ticket_id": job.source_id,
        "source_model": job.source_model,
        "status": job.status,
        "reason_code": job.reason_code,
        "review_state": review_state,
        "latest_review": (
            _review_view(latest_review, current_proposal_digest=digest)
            if latest_review is not None
            else None
        ),
        "category": metadata["category"],
        "zone": metadata["zone"],
        "quality": _quality(job),
        "attempt_count": int(job.attempt_count or 0),
        "last_attempt_at": _iso(job.last_attempt_at),
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "actions": _actions(job),
    }


def _normalized_filter(value: Any, *, allowed: frozenset[str] | None = None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if allowed is not None and normalized not in allowed:
        raise TerritorialGeocodingAdminError(
            "geocoding_queue_filter_invalid",
            action_hint="use_supported_filter",
        )
    if allowed is None and not _SAFE_FILTER_RE.fullmatch(normalized):
        raise TerritorialGeocodingAdminError(
            "geocoding_queue_filter_invalid",
            action_hint="use_supported_filter",
        )
    return normalized


def list_geocoding_queue(
    session: Any,
    *,
    tenant_id: int,
    page: int = 1,
    per_page: int = 25,
    status: Any = None,
    review_state: Any = None,
    source_model: Any = None,
    reason_code: Any = None,
    ticket_id: Any = None,
    category: Any = None,
    zone: Any = None,
    quality_state: Any = None,
) -> dict[str, Any]:
    """Return a redacted, tenant-scoped queue and aggregate summary."""

    try:
        normalized_tenant_id = int(tenant_id)
        normalized_page = max(1, int(page))
        normalized_per_page = max(1, min(int(per_page), 100))
    except (TypeError, ValueError, OverflowError) as exc:
        raise TerritorialGeocodingAdminError(
            "geocoding_queue_pagination_invalid"
        ) from exc
    if normalized_tenant_id <= 0:
        raise TerritorialGeocodingAdminError(
            "geocoding_queue_tenant_invalid", status_code=403
        )

    normalized_status = _normalized_filter(
        status, allowed=TerritorialGeocodingJob.VALID_STATUSES
    )
    normalized_review = _normalized_filter(
        review_state, allowed=VALID_REVIEW_STATES
    )
    normalized_source = _normalized_filter(source_model)
    normalized_reason = _normalized_filter(reason_code)
    normalized_ticket = _normalized_filter(ticket_id)
    normalized_category = _safe_label(category, maximum=96)
    normalized_zone = _safe_label(zone, maximum=120)
    normalized_quality = _normalized_filter(quality_state)

    query = session.query(TerritorialGeocodingJob).filter(
        TerritorialGeocodingJob.tenant_id == normalized_tenant_id
    )
    if normalized_status:
        query = query.filter(TerritorialGeocodingJob.status == normalized_status)
    if normalized_source:
        query = query.filter(
            TerritorialGeocodingJob.source_model == normalized_source
        )
    if normalized_reason:
        query = query.filter(
            TerritorialGeocodingJob.reason_code == normalized_reason
        )
    if normalized_ticket:
        query = query.filter(TerritorialGeocodingJob.source_id == normalized_ticket)

    jobs = query.order_by(
        TerritorialGeocodingJob.updated_at.desc(),
        TerritorialGeocodingJob.created_at.desc(),
        TerritorialGeocodingJob.id.asc(),
    ).all()
    reviews = _latest_reviews(
        session,
        tenant_id=normalized_tenant_id,
        job_ids=[job.id for job in jobs],
    )

    views = [_item_view(job, reviews.get(job.id)) for job in jobs]
    if normalized_review:
        views = [item for item in views if item["review_state"] == normalized_review]
    if normalized_category:
        expected = normalized_category.casefold()
        views = [
            item for item in views if str(item.get("category") or "").casefold() == expected
        ]
    if normalized_zone:
        expected = normalized_zone.casefold()
        views = [item for item in views if str(item.get("zone") or "").casefold() == expected]
    if normalized_quality:
        views = [
            item
            for item in views
            if str((item.get("quality") or {}).get("state") or "").lower()
            == normalized_quality
        ]

    status_counts = Counter(item["status"] for item in views)
    review_counts = Counter(item["review_state"] for item in views)
    reason_counts = Counter(item["reason_code"] for item in views)
    quality_counts = Counter((item["quality"] or {}).get("state") for item in views)
    total = len(views)
    start = (normalized_page - 1) * normalized_per_page
    paged_items = views[start : start + normalized_per_page]

    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": normalized_tenant_id,
        "summary": {
            "total": total,
            "by_status": dict(sorted(status_counts.items())),
            "by_review_state": dict(sorted(review_counts.items())),
            "by_reason_code": dict(sorted(reason_counts.items())),
            "by_quality_state": dict(sorted(quality_counts.items())),
            "with_proposal": sum(
                1 for item in views if (item.get("quality") or {}).get("has_proposal")
            ),
            "needs_human_review": sum(
                1
                for item in views
                if (item.get("quality") or {}).get("state")
                == "requires_human_review"
                and item.get("review_state") in {"unreviewed", "stale"}
            ),
            "coordinate_writes_from_review": 0,
        },
        "filters": {
            "status": normalized_status,
            "review_state": normalized_review,
            "source_model": normalized_source,
            "reason_code": normalized_reason,
            "ticket_id": normalized_ticket,
            "category": normalized_category,
            "zone": normalized_zone,
            "quality_state": normalized_quality,
        },
        "pagination": {
            "page": normalized_page,
            "per_page": normalized_per_page,
            "total": total,
            "has_next": start + normalized_per_page < total,
        },
        "items": paged_items,
        "privacy": {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "exact_coordinates_exposed": False,
            "aggregate_list_only": True,
        },
        "frontend_contract": {
            "render_as": "territorial_geocoding_queue",
            "focus": "open_geocoding_queue",
            "selection_key": "selected",
            "detail_endpoint_template": f"{QUEUE_PATH}/{{job_id}}",
            "empty_state": "no_geocoding_jobs_for_filters",
        },
    }


def _tenant_job(
    session: Any,
    *,
    tenant_id: int,
    job_id: str,
    for_update: bool = False,
) -> TerritorialGeocodingJob:
    query = session.query(TerritorialGeocodingJob).filter(
        TerritorialGeocodingJob.tenant_id == int(tenant_id),
        TerritorialGeocodingJob.id == str(job_id),
    )
    if for_update:
        # PostgreSQL serializes a human decision against concurrent job
        # transitions. SQLite ignores this in focal tests without weakening the
        # tenant predicate or the immutable receipt constraints.
        query = query.with_for_update()
    job = query.one_or_none()
    if job is None:
        # Tenant mismatches intentionally look identical to unknown jobs.
        raise TerritorialGeocodingAdminError(
            "geocoding_job_not_found",
            status_code=404,
            action_hint="refresh_geocoding_queue",
        )
    return job


def _attempt_view(attempt: TerritorialGeocodingAttempt) -> dict[str, Any]:
    result = _mapping(attempt.result_json)
    proposal = _mapping(result.get("proposal"))
    validation = _mapping(result.get("validation"))
    return {
        "id": attempt.id,
        "attempt_number": int(attempt.attempt_number),
        "outcome_status": attempt.outcome_status,
        "reason_code": attempt.reason_code,
        "provider": attempt.provider,
        "external_call_performed": bool(attempt.external_call_performed),
        "coordinate_write_performed": bool(attempt.write_performed),
        "proposal": {
            "lat": _finite_float(proposal.get("lat")),
            "lng": _finite_float(proposal.get("lng")),
            "location_type": _safe_label(
                proposal.get("location_type"), maximum=32
            ),
            "partial_match": (
                bool(proposal.get("partial_match")) if proposal else None
            ),
        }
        if proposal
        else None,
        "validation": {
            "auto_apply_eligible": bool(validation.get("auto_apply_eligible")),
            "issues": [
                _safe_label(issue, maximum=96)
                for issue in (validation.get("issues") or [])
                if _safe_label(issue, maximum=96)
            ][:20],
            "jurisdiction_status": _safe_label(
                validation.get("jurisdiction_status"), maximum=32
            ),
            "locality_match": validation.get("locality_match"),
            "province_match": validation.get("province_match"),
            "country_match": validation.get("country_match"),
        },
        "created_at": _iso(attempt.created_at),
    }


def geocoding_job_detail(
    session: Any,
    *,
    tenant_id: int,
    job_id: str,
) -> dict[str, Any]:
    job = _tenant_job(session, tenant_id=tenant_id, job_id=job_id)
    reviews = (
        session.query(TerritorialGeocodingReview)
        .filter(
            TerritorialGeocodingReview.tenant_id == int(tenant_id),
            TerritorialGeocodingReview.job_id == job.id,
        )
        .order_by(
            TerritorialGeocodingReview.created_at.desc(),
            TerritorialGeocodingReview.id.desc(),
        )
        .all()
    )
    attempts = (
        session.query(TerritorialGeocodingAttempt)
        .filter(
            TerritorialGeocodingAttempt.tenant_id == int(tenant_id),
            TerritorialGeocodingAttempt.job_id == job.id,
        )
        .order_by(TerritorialGeocodingAttempt.attempt_number.desc())
        .all()
    )
    current_digest = proposal_digest(job)
    item = _item_view(job, reviews[0] if reviews else None)
    proposal = _proposal_snapshot(job)
    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": int(tenant_id),
        "item": item,
        "selected": {
            "proposal": proposal,
            "proposal_digest": current_digest,
            "attempts": [_attempt_view(attempt) for attempt in attempts],
            "reviews": [
                _review_view(review, current_proposal_digest=current_digest)
                for review in reviews
            ],
        },
        "privacy": {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "exact_coordinates_exposed": bool(
                proposal["lat"] is not None and proposal["lng"] is not None
            ),
            "authorized_admin_detail": True,
        },
        "write_policy": {
            "get_is_read_only": True,
            "provider_call_performed": False,
            "coordinate_application_supported": False,
            "review_is_human_decision_only": True,
        },
    }


def geocoding_job_attempts(
    session: Any,
    *,
    tenant_id: int,
    job_id: str,
) -> dict[str, Any]:
    job = _tenant_job(session, tenant_id=tenant_id, job_id=job_id)
    attempts = (
        session.query(TerritorialGeocodingAttempt)
        .filter(
            TerritorialGeocodingAttempt.tenant_id == int(tenant_id),
            TerritorialGeocodingAttempt.job_id == job.id,
        )
        .order_by(TerritorialGeocodingAttempt.attempt_number.desc())
        .all()
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": int(tenant_id),
        "job_id": job.id,
        "ticket_id": job.source_id,
        "source_model": job.source_model,
        "attempts": [_attempt_view(attempt) for attempt in attempts],
        "privacy": {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "authorized_admin_detail": True,
        },
    }


def _idempotency_key(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(normalized):
        raise TerritorialGeocodingAdminError(
            "geocoding_review_idempotency_key_invalid",
            action_hint="send_valid_idempotency_key",
        )
    return normalized


def _review_payload(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise TerritorialGeocodingAdminError(
            "geocoding_review_payload_invalid"
        )
    supported_fields = {"decision", "reason_code", "apply_coordinates"}
    if set(payload) - supported_fields:
        raise TerritorialGeocodingAdminError(
            "geocoding_review_payload_fields_unsupported",
            action_hint="remove_unsupported_fields",
        )
    if "apply_coordinates" in payload and payload.get("apply_coordinates") is not False:
        raise TerritorialGeocodingAdminError(
            "geocoding_review_coordinate_application_not_supported",
            status_code=409,
            action_hint="submit_human_review_without_coordinate_write",
            message=(
                "La revisión humana no aplica coordenadas; use el flujo de "
                "escritura territorial con autoridad global verificada."
            ),
        )
    decision = str(payload.get("decision") or "").strip().lower()
    reason_code = str(payload.get("reason_code") or "").strip().lower()
    if decision not in REVIEW_REASON_CODES:
        raise TerritorialGeocodingAdminError(
            "geocoding_review_decision_invalid",
            action_hint="use_approved_or_rejected",
        )
    if reason_code not in REVIEW_REASON_CODES[decision]:
        raise TerritorialGeocodingAdminError(
            "geocoding_review_reason_code_invalid",
            action_hint="use_reason_code_for_decision",
        )
    return decision, reason_code


def review_geocoding_job(
    session: Any,
    *,
    tenant_id: int,
    job_id: str,
    reviewer_user_id: int,
    idempotency_key: Any,
    payload: Any,
) -> dict[str, Any]:
    """Append one idempotent human review; never call provider or write coords."""

    job = _tenant_job(
        session,
        tenant_id=tenant_id,
        job_id=job_id,
        for_update=True,
    )
    decision, reason_code = _review_payload(payload)
    key = _idempotency_key(idempotency_key)
    key_hash = _canonical_digest(
        {
            "scope": "territorial_geocoding_review",
            "tenant_id": int(tenant_id),
            "job_id": job.id,
            "key": key,
        }
    )
    current_proposal_digest = proposal_digest(job)
    request_digest = _canonical_digest(
        {
            "tenant_id": int(tenant_id),
            "job_id": job.id,
            "reviewer_user_id": int(reviewer_user_id),
            "decision": decision,
            "reason_code": reason_code,
            "proposal_digest": current_proposal_digest,
        }
    )

    existing = (
        session.query(TerritorialGeocodingReview)
        .filter(
            TerritorialGeocodingReview.tenant_id == int(tenant_id),
            TerritorialGeocodingReview.job_id == job.id,
            TerritorialGeocodingReview.idempotency_key_hash == key_hash,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.request_digest != request_digest:
            raise TerritorialGeocodingAdminError(
                "geocoding_review_idempotency_conflict",
                status_code=409,
                action_hint="reuse_original_request_or_new_idempotency_key",
            )
        return {
            "contract_version": CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "job_id": job.id,
            "review": _review_view(
                existing, current_proposal_digest=current_proposal_digest
            ),
            "idempotent_replay": True,
            "provider_call_performed": False,
            "coordinate_write_performed": False,
        }

    if job.status == "applied":
        raise TerritorialGeocodingAdminError(
            "geocoding_job_already_applied",
            status_code=409,
            action_hint="inspect_applied_job",
        )

    if decision == "approved" and not _quality(job)["has_proposal"]:
        raise TerritorialGeocodingAdminError(
            "geocoding_proposal_missing",
            status_code=409,
            action_hint="inspect_attempts_or_reject_job",
        )

    review = TerritorialGeocodingReview(
        tenant_id=int(tenant_id),
        job_id=job.id,
        reviewer_user_id=int(reviewer_user_id),
        contract_version=CONTRACT_VERSION,
        decision=decision,
        reason_code=reason_code,
        reviewed_job_status=job.status,
        proposal_digest=current_proposal_digest,
        idempotency_key_hash=key_hash,
        request_digest=request_digest,
        coordinate_write_performed=False,
    )
    session.add(review)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raced = (
            session.query(TerritorialGeocodingReview)
            .filter(
                TerritorialGeocodingReview.tenant_id == int(tenant_id),
                TerritorialGeocodingReview.job_id == str(job_id),
                TerritorialGeocodingReview.idempotency_key_hash == key_hash,
            )
            .one_or_none()
        )
        if raced is None or raced.request_digest != request_digest:
            raise TerritorialGeocodingAdminError(
                "geocoding_review_idempotency_conflict",
                status_code=409,
                action_hint="reuse_original_request_or_new_idempotency_key",
            )
        review = raced
        replayed = True
    else:
        replayed = False

    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": int(tenant_id),
        "job_id": str(job_id),
        "review": _review_view(
            review, current_proposal_digest=current_proposal_digest
        ),
        "idempotent_replay": replayed,
        "provider_call_performed": False,
        "coordinate_write_performed": False,
    }


__all__ = [
    "APPROVAL_REASON_CODES",
    "CONTRACT_VERSION",
    "QUEUE_PATH",
    "REJECTION_REASON_CODES",
    "TerritorialGeocodingAdminError",
    "geocoding_job_attempts",
    "geocoding_job_detail",
    "list_geocoding_queue",
    "proposal_digest",
    "review_geocoding_job",
]
