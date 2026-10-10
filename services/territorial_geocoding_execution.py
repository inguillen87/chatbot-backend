from __future__ import annotations

"""Audited execution boundary for territorial geocoding jobs.

This module is intentionally the only bridge between the durable queue, a
geocoding provider and ticket coordinate writes.  It rehydrates every source
record from the database and never trusts the queue snapshot as write input.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import re
from typing import Any, Callable

from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket
from models_territorial_geocoding import (
    TerritorialGeocodingAttempt,
    TerritorialGeocodingJob,
    TerritorialGeocodingReview,
)
from services.tenant_ticket_scope import municipio_ticket_belongs_to_tenant
from services.territorial_evidence import (
    coordinate_jurisdiction_status,
    extract_location_evidence,
    resolve_tenant_jurisdiction,
)
from services.territorial_geocoding import (
    TerritorialGeocodingCandidate,
    TerritorialGeocodingProviderReceipt,
    build_territorial_geocoding_candidate,
    evaluate_geocoding_result,
)
from services.territorial_geocoding_admin import (
    TerritorialGeocodingAdminError,
    proposal_digest,
)
from services.territorial_geocoding_store import (
    SQLAlchemyTerritorialGeocodingAuditStore,
    build_ticket_coordinate_applier,
)


CONTRACT_VERSION = "operations.territorial_geocoding_execution.v1"
METRICS_CONTRACT_VERSION = "operations.territorial_geocoding_transition_metrics.v1"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_MAX_PROVIDER_ADDRESS_CHARS = 512
_LOGGER = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key(value: Any, *, action: str) -> tuple[str, str]:
    key = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise TerritorialGeocodingAdminError(
            f"geocoding_{action}_idempotency_key_invalid",
            action_hint="send_valid_idempotency_key",
            message="Idempotency-Key es obligatorio y debe tener entre 8 y 128 caracteres seguros.",
        )
    return key, _digest({"scope": f"territorial_geocoding_{action}", "key": key})


def _tenant_job(
    session: Any,
    *,
    tenant_id: int,
    job_id: str,
) -> TerritorialGeocodingJob:
    job = (
        session.query(TerritorialGeocodingJob)
        .filter(
            TerritorialGeocodingJob.tenant_id == int(tenant_id),
            TerritorialGeocodingJob.id == str(job_id),
        )
        .with_for_update()
        .one_or_none()
    )
    if job is None:
        raise TerritorialGeocodingAdminError(
            "geocoding_job_not_found",
            status_code=404,
            action_hint="refresh_geocoding_queue",
        )
    return job


def _source_record(
    session: Any,
    *,
    tenant: TenantProfile,
    source_model: str,
    source_id: str,
    for_update: bool = False,
) -> Any:
    try:
        record_id = int(source_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TerritorialGeocodingAdminError(
            "geocoding_source_identity_invalid",
            status_code=409,
            action_hint="refresh_geocoding_queue",
        ) from exc

    normalized_source = str(source_model or "").strip().lower()
    if normalized_source == "tenant_ticket":
        query = session.query(TenantTicket).filter_by(
            id=record_id, tenant_id=int(tenant.id)
        )
        record = (query.with_for_update() if for_update else query).one_or_none()
    elif normalized_source == "pyme_ticket":
        query = session.query(PymeTicket).filter_by(
            id=record_id, tenant_id=int(tenant.id)
        )
        record = (query.with_for_update() if for_update else query).one_or_none()
    elif normalized_source == "municipio_ticket":
        query = session.query(MunicipioTicket).filter_by(
            id=record_id, tenant_id=int(tenant.id)
        )
        record = (query.with_for_update() if for_update else query).one_or_none()
        if record is None:
            legacy_query = session.query(MunicipioTicket).filter_by(id=record_id)
            record = (
                legacy_query.with_for_update() if for_update else legacy_query
            ).one_or_none()
        if not municipio_ticket_belongs_to_tenant(record, tenant):
            record = None
    else:
        record = None
    if record is None:
        # A tenant mismatch is deliberately indistinguishable from a deleted
        # source record.
        raise TerritorialGeocodingAdminError(
            "geocoding_source_not_found",
            status_code=404,
            action_hint="refresh_geocoding_queue",
        )
    return record


def _record_evidence(source_model: str, record: Any) -> dict[str, Any]:
    normalized_source = str(source_model or "").strip().lower()
    metadata = getattr(record, "datos_extra", None) or {}
    if normalized_source == "tenant_ticket":
        location = extract_location_evidence(("ticket_metadata", metadata))
        address = location.get("address")
        zone = location.get("zone")
    else:
        address = getattr(record, "direccion", None)
        zone = getattr(record, "distrito", None)
    return {
        "record_source": normalized_source,
        "record_id": str(getattr(record, "id", "")),
        "address": address,
        "lat": getattr(record, "latitud", None),
        "lng": getattr(record, "longitud", None),
        "category": getattr(record, "categoria", None),
        "zone": zone,
    }


def _rehydrate_candidate(
    session: Any,
    *,
    tenant: TenantProfile,
    job: TerritorialGeocodingJob,
    lock_source: bool = False,
) -> tuple[TerritorialGeocodingCandidate, dict[str, Any], Any]:
    record = _source_record(
        session,
        tenant=tenant,
        source_model=job.source_model,
        source_id=job.source_id,
        for_update=lock_source,
    )
    if lock_source:
        if (
            getattr(record, "latitud", None) is not None
            or getattr(record, "longitud", None) is not None
        ):
            raise TerritorialGeocodingAdminError(
                "geocoding_source_coordinates_already_present",
                status_code=409,
                action_hint="inspect_current_ticket_coordinates",
                message="El reclamo ya tiene coordenadas y no puede sobrescribirse desde esta propuesta.",
            )
        metadata = getattr(record, "datos_extra", None)
        persisted_provenance = (
            metadata.get("territorial_coordinate_provenance")
            if isinstance(metadata, dict)
            else None
        )
        if persisted_provenance is not None:
            raise TerritorialGeocodingAdminError(
                "geocoding_source_provenance_conflict",
                status_code=409,
                action_hint="inspect_coordinate_provenance",
                message="El reclamo conserva una procedencia territorial incompatible y requiere revisión.",
            )
    jurisdiction = resolve_tenant_jurisdiction(tenant)
    candidate = build_territorial_geocoding_candidate(
        _record_evidence(job.source_model, record),
        tenant_id=int(tenant.id),
        tenant_slug=str(tenant.slug or ""),
        jurisdiction=jurisdiction,
    )
    if candidate is None:
        raise TerritorialGeocodingAdminError(
            "geocoding_source_no_longer_pending",
            status_code=409,
            action_hint="refresh_geocoding_queue",
            message="El reclamo ya no conserva una dirección pendiente sin coordenadas.",
        )
    if candidate.fingerprint != job.candidate_fingerprint:
        raise TerritorialGeocodingAdminError(
            "geocoding_candidate_stale",
            status_code=409,
            action_hint="sync_geocoding_queue",
            message="La dirección o la jurisdicción cambió; actualizá la cola antes de continuar.",
        )
    return candidate, jurisdiction, record


def _request_digest(
    *,
    action: str,
    tenant_id: int,
    job: TerritorialGeocodingJob,
    actor_user_id: int,
    key_hash: str,
    current_proposal_digest: str | None,
    proposal_attempt_id: str | None = None,
    proposal_attempt_number: int | None = None,
) -> str:
    return _digest(
        {
            "contract_version": CONTRACT_VERSION,
            "action": action,
            "tenant_id": int(tenant_id),
            "job_id": job.id,
            "actor_user_id": int(actor_user_id),
            "candidate_fingerprint": job.candidate_fingerprint,
            "proposal_digest": current_proposal_digest,
            "proposal_attempt_id": proposal_attempt_id,
            "proposal_attempt_number": proposal_attempt_number,
            "idempotency_key_hash": key_hash,
        }
    )


def _idempotent_result(
    session: Any,
    *,
    job: TerritorialGeocodingJob,
    action: str,
    key_hash: str,
    request_digest: str,
) -> dict[str, Any] | None:
    attempt = (
        session.query(TerritorialGeocodingAttempt)
        .filter(
            TerritorialGeocodingAttempt.job_id == job.id,
            TerritorialGeocodingAttempt.action == action,
            TerritorialGeocodingAttempt.idempotency_key_hash == key_hash,
        )
        .one_or_none()
    )
    attempts = [attempt] if attempt is not None else (
        session.query(TerritorialGeocodingAttempt)
        .filter(
            TerritorialGeocodingAttempt.job_id == job.id,
            TerritorialGeocodingAttempt.action.is_(None),
            TerritorialGeocodingAttempt.idempotency_key_hash.is_(None),
        )
        .order_by(TerritorialGeocodingAttempt.attempt_number.desc())
        .all()
    )
    for attempt in attempts:
        result = attempt.result_json if isinstance(attempt.result_json, dict) else {}
        execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
        if execution.get("action") != action or execution.get("idempotency_key_hash") != key_hash:
            continue
        if attempt.request_digest != request_digest:
            raise TerritorialGeocodingAdminError(
                f"geocoding_{action}_idempotency_conflict",
                status_code=409,
                action_hint="reuse_original_request_or_new_idempotency_key",
            )
        replay = dict(result)
        replay["idempotent_replay"] = True
        return replay
    return None


def _current_proposal_attempt(
    session: Any,
    *,
    tenant_id: int,
    job_id: str,
    lock: bool = False,
) -> TerritorialGeocodingAttempt | None:
    query = (
        session.query(TerritorialGeocodingAttempt)
        .filter(
            TerritorialGeocodingAttempt.tenant_id == int(tenant_id),
            TerritorialGeocodingAttempt.job_id == str(job_id),
            TerritorialGeocodingAttempt.action == "resolve",
        )
        .order_by(
            TerritorialGeocodingAttempt.attempt_number.desc(),
            TerritorialGeocodingAttempt.id.desc(),
        )
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def _transition(from_status: str, to_status: str, *, action: str) -> dict[str, Any]:
    # Deliberately excludes ticket ids, job ids, actor ids, addresses and
    # coordinates so it is safe for aggregate operational metrics.
    return {
        "contract_version": METRICS_CONTRACT_VERSION,
        "action": action,
        "from_status": str(from_status),
        "to_status": str(to_status),
    }


def _response(
    *,
    tenant_id: int,
    tenant_slug: str,
    job_id: str,
    action: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    proposal = outcome.get("proposal") if isinstance(outcome.get("proposal"), dict) else None
    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": int(tenant_id),
        "tenant_slug": str(tenant_slug or "").strip().lower(),
        "job_id": str(job_id),
        "action": action,
        "status": outcome.get("status"),
        "reason_code": outcome.get("reason_code"),
        "proposal_digest": outcome.get("proposal_digest"),
        "proposal_version": outcome.get("proposal_version"),
        "proposal": proposal,
        "validation": outcome.get("validation") or {},
        "execution": {
            "provider_call_performed": bool(outcome.get("external_call_performed")),
            "coordinate_write_performed": bool(outcome.get("write_performed")),
            "coordinates_applied": bool(
                outcome.get("status") == "applied"
                and outcome.get("reason_code") == "coordinates_applied"
                and outcome.get("write_performed")
            ),
            "write_performed": bool(outcome.get("write_performed")),
        },
        "transition_metrics": outcome.get("transition_metrics"),
        "idempotent_replay": bool(outcome.get("idempotent_replay")),
        "privacy": {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "provider_payload_exposed": False,
            "authorized_admin_detail": True,
            "exact_coordinates_classification": "restricted_operational",
            "exact_coordinates_access": "tenant_admin_only",
            "provider_place_id_retained": False,
            "source_address_retained_in_audit": False,
        },
    }


def _bounded_provider_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return 5.0
    if not math.isfinite(timeout):
        return 5.0
    return min(max(timeout, 1.0), 10.0)


def _stamp_execution_receipt(
    session: Any,
    *,
    job: TerritorialGeocodingJob,
    attempt: TerritorialGeocodingAttempt,
    outcome: dict[str, Any],
    proposal_attempt: TerritorialGeocodingAttempt,
) -> None:
    """Persist the proposal identity used by both approval and UI receipts."""

    outcome["proposal_digest"] = proposal_digest(job)
    outcome["proposal_version"] = {
        "attempt_id": proposal_attempt.id,
        "attempt_number": int(proposal_attempt.attempt_number),
    }
    outcome["execution_attempt"] = {
        "attempt_id": attempt.id,
        "attempt_number": int(attempt.attempt_number),
    }
    outcome["privacy"] = {
        "exact_coordinates_classification": "restricted_operational",
        "exact_coordinates_access": "tenant_admin_only",
        "provider_place_id_retained": False,
        "provider_payload_retained": False,
        "source_address_retained": False,
    }
    persisted = json.loads(json.dumps(outcome, ensure_ascii=True, default=str))
    attempt.result_json = persisted
    attempt.result_digest = _digest(persisted)
    job.result_json = persisted
    session.flush()


def resolve_territorial_geocoding_job(
    session: Any,
    *,
    tenant: TenantProfile,
    job_id: str,
    actor_user_id: int,
    idempotency_key: Any,
    geocoder: Callable[[str, dict[str, Any]], Any],
    provider_name: str = "google",
    provider_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Consult one bounded provider and persist a proposal; never write a ticket."""

    _key, key_hash = _idempotency_key(idempotency_key, action="resolve")
    job = _tenant_job(session, tenant_id=int(tenant.id), job_id=job_id)
    digest = _request_digest(
        action="resolve",
        tenant_id=int(tenant.id),
        job=job,
        actor_user_id=actor_user_id,
        key_hash=key_hash,
        current_proposal_digest=None,
    )
    replay = _idempotent_result(
        session,
        job=job,
        action="resolve",
        key_hash=key_hash,
        request_digest=digest,
    )
    if replay is not None:
        return _response(
            tenant_id=int(tenant.id), tenant_slug=tenant.slug,
            job_id=job.id, action="resolve", outcome=replay
        )

    # A durable key replays before touching mutable source state. This keeps a
    # historical receipt available even if the ticket was completed, corrected
    # or deleted after the original provider attempt, and it guarantees an old
    # key can never cross the provider boundary again.
    candidate, _jurisdiction, _record = _rehydrate_candidate(
        session, tenant=tenant, job=job
    )
    if len(candidate.address) > _MAX_PROVIDER_ADDRESS_CHARS:
        raise TerritorialGeocodingAdminError(
            "geocoding_provider_input_too_long",
            status_code=409,
            action_hint="correct_source_address",
        )
    timeout = _bounded_provider_timeout(provider_timeout_seconds)
    context = {**candidate.provider_context, "provider_timeout_seconds": timeout, "max_results": 1}
    previous_status = str(job.status)
    provider_call_performed = False
    try:
        provider_result = geocoder(candidate.address, context)
    except Exception:
        # The production adapter reports the provider-call boundary explicitly.
        # If an adapter escapes without a receipt, no external call can be
        # truthfully attested; fail closed instead of inventing a provider call.
        outcome = {
            "status": "failed",
            "reason_code": "provider_request_failed",
            "provider": provider_name,
            "proposal": None,
            "validation": {
                "auto_apply_eligible": False,
                "issues": ["provider_request_failed"],
            },
        }
    else:
        if isinstance(provider_result, TerritorialGeocodingProviderReceipt):
            provider_payload = provider_result.payload
            provider_call_performed = bool(
                provider_result.external_call_performed
            )
        else:
            # Third-party/test adapters implement the provider call directly;
            # entering them is the externally observable attempt boundary.
            provider_payload = provider_result
            provider_call_performed = True
        try:
            outcome = evaluate_geocoding_result(
                candidate, provider_payload, provider_name=provider_name
            )
        except Exception:
            # Result parsing happens after the adapter's explicit receipt. Keep
            # the receipt's external-call truth unchanged if parsing fails.
            outcome = {
                "status": "failed",
                "reason_code": "provider_result_invalid",
                "provider": provider_name,
                "proposal": None,
                "validation": {
                    "auto_apply_eligible": False,
                    "issues": ["provider_result_invalid"],
                },
            }
    outcome = {
        **outcome,
        "contract_version": "operations.territorial_geocoding.v1",
        "candidate": candidate.audit_identity(),
        "request_digest": digest,
        "external_call_performed": provider_call_performed,
        "write_performed": False,
        "dry_run": True,
        "idempotent_replay": False,
        "execution": {
            "action": "resolve",
            "idempotency_key_hash": key_hash,
            "provider_timeout_seconds": timeout,
            "provider_result_limit": 1,
        },
    }
    outcome["transition_metrics"] = _transition(
        previous_status, str(outcome["status"]), action="resolve"
    )
    attempt = SQLAlchemyTerritorialGeocodingAuditStore(session).record_attempt(
        candidate,
        digest,
        outcome,
        external_call_performed=provider_call_performed,
        write_performed=False,
    )
    _stamp_execution_receipt(
        session,
        job=job,
        attempt=attempt,
        outcome=outcome,
        proposal_attempt=attempt,
    )
    _LOGGER.info(
        "territorial_geocoding_transition action=resolve from_status=%s to_status=%s",
        previous_status,
        outcome["status"],
    )
    return _response(
        tenant_id=int(tenant.id), tenant_slug=tenant.slug,
        job_id=job.id, action="resolve", outcome=outcome
    )


def apply_territorial_geocoding_job(
    session: Any,
    *,
    tenant: TenantProfile,
    job_id: str,
    actor_user_id: int,
    idempotency_key: Any,
    writer_authority_confirmed: bool,
    writer_authority_reason: str,
    writer_authority_epoch: int | None,
    expected_proposal_digest: str,
    expected_attempt_id: str,
    expected_attempt_number: int,
) -> dict[str, Any]:
    """Apply only the current approved proposal inside the caller transaction."""

    _key, key_hash = _idempotency_key(idempotency_key, action="apply")
    job = _tenant_job(session, tenant_id=int(tenant.id), job_id=job_id)
    current_proposal_digest = proposal_digest(job)
    current_proposal_attempt = _current_proposal_attempt(
        session,
        tenant_id=int(tenant.id),
        job_id=job.id,
        lock=True,
    )
    normalized_expected_digest = str(expected_proposal_digest or "").strip().lower()
    normalized_expected_attempt_id = str(expected_attempt_id or "").strip()
    if (
        normalized_expected_digest != current_proposal_digest
        or normalized_expected_attempt_id == ""
        or isinstance(expected_attempt_number, bool)
        or current_proposal_attempt is None
    ):
        raise TerritorialGeocodingAdminError(
            "geocoding_apply_expected_version_invalid",
            status_code=409,
            action_hint="refresh_geocoding_detail",
        )
    try:
        normalized_expected_attempt_number = int(expected_attempt_number)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TerritorialGeocodingAdminError(
            "geocoding_apply_expected_version_invalid",
            status_code=409,
            action_hint="refresh_geocoding_detail",
        ) from exc
    if (
        normalized_expected_attempt_id != current_proposal_attempt.id
        or normalized_expected_attempt_number
        != current_proposal_attempt.attempt_number
    ):
        raise TerritorialGeocodingAdminError(
            "geocoding_apply_expected_version_stale",
            status_code=409,
            action_hint="refresh_geocoding_detail",
        )
    digest = _request_digest(
        action="apply",
        tenant_id=int(tenant.id),
        job=job,
        actor_user_id=actor_user_id,
        key_hash=key_hash,
        current_proposal_digest=current_proposal_digest,
        proposal_attempt_id=current_proposal_attempt.id,
        proposal_attempt_number=current_proposal_attempt.attempt_number,
    )
    replay = _idempotent_result(
        session,
        job=job,
        action="apply",
        key_hash=key_hash,
        request_digest=digest,
    )
    if replay is not None:
        return _response(
            tenant_id=int(tenant.id), tenant_slug=tenant.slug,
            job_id=job.id, action="apply", outcome=replay
        )
    if job.status == "applied":
        raise TerritorialGeocodingAdminError(
            "geocoding_job_already_applied",
            status_code=409,
            action_hint="inspect_applied_job",
        )

    candidate, jurisdiction, locked_record = _rehydrate_candidate(
        session, tenant=tenant, job=job, lock_source=True
    )
    validation = job.validation_json if isinstance(job.validation_json, dict) else {}
    proposal = {
        "lat": job.proposed_lat,
        "lng": job.proposed_lng,
        "coordinate_reference": "WGS84",
        "location_type": job.location_type,
        "partial_match": job.partial_match,
        "provider_reference_present": bool(
            (
                job.result_json.get("proposal", {})
                if isinstance(job.result_json, dict)
                and isinstance(job.result_json.get("proposal"), dict)
                else {}
            ).get("provider_reference_present")
            or job.provider_place_id
        ),
        "provenance": {
            "contract_version": "operations.coordinate_provenance.v1",
            "source": "approved_geocoding_proposal",
            "provider": job.provider,
            "proposal_digest": current_proposal_digest,
            "source_address_retained": False,
        },
    }
    if (
        proposal["lat"] is None
        or proposal["lng"] is None
        or not bool(validation.get("auto_apply_eligible"))
    ):
        raise TerritorialGeocodingAdminError(
            "geocoding_proposal_not_auto_apply_eligible",
            status_code=409,
            action_hint="resolve_or_review_proposal",
        )

    latest_review = (
        session.query(TerritorialGeocodingReview)
        .filter(
            TerritorialGeocodingReview.tenant_id == int(tenant.id),
            TerritorialGeocodingReview.job_id == job.id,
        )
        .order_by(
            TerritorialGeocodingReview.created_at.desc(),
            TerritorialGeocodingReview.id.desc(),
        )
        .first()
    )
    if (
        latest_review is None
        or current_proposal_attempt is None
        or latest_review.decision != "approved"
        or latest_review.proposal_digest != current_proposal_digest
        or latest_review.proposal_attempt_id != current_proposal_attempt.id
        or latest_review.proposal_attempt_number
        != current_proposal_attempt.attempt_number
    ):
        raise TerritorialGeocodingAdminError(
            "geocoding_current_human_approval_required",
            status_code=409,
            action_hint="approve_current_proposal",
        )

    authority = jurisdiction.get("boundary_authority")
    official_polygon = bool(
        jurisdiction.get("containment_verified")
        and jurisdiction.get("containment_method") == "point_in_polygon"
        and isinstance(authority, dict)
        and str(authority.get("kind") or "").strip().lower() == "official"
    )
    if not official_polygon:
        raise TerritorialGeocodingAdminError(
            "geocoding_official_polygon_required",
            status_code=409,
            action_hint="configure_verified_official_boundary",
        )
    if coordinate_jurisdiction_status(
        proposal["lat"], proposal["lng"], jurisdiction
    ) != "within":
        raise TerritorialGeocodingAdminError(
            "geocoding_proposal_outside_official_polygon",
            status_code=409,
            action_hint="resolve_or_reject_proposal",
        )
    if (
        not writer_authority_confirmed
        or isinstance(writer_authority_epoch, bool)
        or not isinstance(writer_authority_epoch, int)
        or writer_authority_epoch < 0
    ):
        raise TerritorialGeocodingAdminError(
            "geocoding_writer_authority_unconfirmed",
            status_code=503,
            action_hint="restore_global_writer_authority",
            message="La autoridad global de escritura no está disponible.",
        )

    previous_status = str(job.status)
    if not build_ticket_coordinate_applier(
        session, locked_source_record=locked_record
    )(candidate, proposal):
        raise TerritorialGeocodingAdminError(
            "geocoding_coordinate_write_rejected",
            status_code=409,
            action_hint="refresh_geocoding_queue",
        )
    outcome = {
        "contract_version": "operations.territorial_geocoding.v1",
        "status": "applied",
        "reason_code": "coordinates_applied",
        "provider": job.provider,
        "proposal": proposal,
        "validation": validation,
        "candidate": candidate.audit_identity(),
        "request_digest": digest,
        "external_call_performed": False,
        "write_performed": True,
        "dry_run": False,
        "idempotent_replay": False,
        "execution": {
            "action": "apply",
            "idempotency_key_hash": key_hash,
            "human_review_id": latest_review.id,
            "proposal_attempt_id": current_proposal_attempt.id,
            "proposal_attempt_number": current_proposal_attempt.attempt_number,
            "writer_authority_reason": str(writer_authority_reason or "unknown"),
            "writer_authority_epoch": int(writer_authority_epoch),
            "official_polygon_verified": True,
        },
    }
    outcome["transition_metrics"] = _transition(
        previous_status, "applied", action="apply"
    )
    attempt = SQLAlchemyTerritorialGeocodingAuditStore(session).record_attempt(
        candidate,
        digest,
        outcome,
        external_call_performed=False,
        write_performed=True,
    )
    _stamp_execution_receipt(
        session,
        job=job,
        attempt=attempt,
        outcome=outcome,
        proposal_attempt=current_proposal_attempt,
    )
    _LOGGER.info(
        "territorial_geocoding_transition action=apply from_status=%s to_status=applied",
        previous_status,
    )
    return _response(
        tenant_id=int(tenant.id), tenant_slug=tenant.slug,
        job_id=job.id, action="apply", outcome=outcome
    )


__all__ = [
    "CONTRACT_VERSION",
    "METRICS_CONTRACT_VERSION",
    "apply_territorial_geocoding_job",
    "resolve_territorial_geocoding_job",
]
