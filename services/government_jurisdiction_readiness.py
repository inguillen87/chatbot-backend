"""Audited jurisdiction readiness for reusable government tenants.

Tenant administrators may submit an opaque evidence locator plus a document
digest.  Only an independently authorized platform reviewer can verify or
reject that exact submission.  The service deliberately stores neither the
evidence document nor credentials/URLs in the audit trail.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import AuditEvent, TenantProfile
from services.survey_jurisdiction import (
    tenant_requires_government_survey_evidence,
    tenant_verified_jurisdiction,
)


READINESS_CONTRACT_VERSION = "government.jurisdiction.readiness.v1"
SUBMISSION_CONTRACT_VERSION = "government.jurisdiction.evidence_submission.v1"
REVIEW_CONTRACT_VERSION = "government.jurisdiction.review.v1"
AUDIT_CONTRACT_VERSION = "government.jurisdiction.audit.v1"

EVENT_EVIDENCE_SUBMITTED = "tenant_jurisdiction_evidence_submitted"
EVENT_VERIFIED = "tenant_jurisdiction_verified"
EVENT_REJECTED = "tenant_jurisdiction_rejected"
_EVENT_TYPES = frozenset({EVENT_EVIDENCE_SUBMITTED, EVENT_VERIFIED, EVENT_REJECTED})

_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,199}$")
_JURISDICTION_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,159}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,79}$")


class GovernmentJurisdictionError(ValueError):
    def __init__(
        self,
        reason_code: str,
        message: str,
        status_code: int = 409,
        *,
        next_action: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self.next_action = next_action
        self.details = dict(details or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_digest(value: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json(value))


def _normalize_sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise GovernmentJurisdictionError(
            "jurisdiction_evidence_hash_invalid",
            f"{field} debe ser un SHA-256 hexadecimal.",
            422,
            next_action="submit_valid_jurisdiction_evidence",
        )
    return normalized


def _normalize_jurisdiction_ref(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _JURISDICTION_REF_RE.fullmatch(normalized):
        raise GovernmentJurisdictionError(
            "jurisdiction_reference_invalid",
            "jurisdiction_ref debe ser una referencia institucional opaca.",
            422,
            next_action="submit_valid_jurisdiction_evidence",
        )
    return normalized


def _normalize_evidence_ref(value: Any) -> str:
    normalized = str(value or "").strip()
    # Evidence references are storage handles, never URLs, signed links,
    # credentials, email addresses or filesystem traversal paths.
    if (
        not _OPAQUE_REF_RE.fullmatch(normalized)
        or "://" in normalized
        or "@" in normalized
        or "?" in normalized
        or "#" in normalized
        or ".." in normalized
    ):
        raise GovernmentJurisdictionError(
            "jurisdiction_evidence_reference_invalid",
            "evidence_ref debe ser un identificador opaco sin credenciales ni URL.",
            422,
            next_action="submit_valid_jurisdiction_evidence",
        )
    return normalized


def _normalize_idempotency_key(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(normalized):
        raise GovernmentJurisdictionError(
            "jurisdiction_idempotency_key_invalid",
            "Idempotency-Key debe contener entre 8 y 128 caracteres seguros.",
            400,
            next_action="send_valid_idempotency_key",
        )
    return normalized


def _normalize_rejection_reason(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if not _REASON_CODE_RE.fullmatch(normalized):
        raise GovernmentJurisdictionError(
            "jurisdiction_rejection_reason_invalid",
            "reason_code es obligatorio para rechazar y debe ser un codigo seguro.",
            422,
            next_action="send_safe_rejection_reason_code",
        )
    return normalized


def _event_details(event: AuditEvent | None) -> dict[str, Any]:
    return dict(event.details) if event is not None and isinstance(event.details, dict) else {}


def _jurisdiction_events(tenant_id: int) -> list[AuditEvent]:
    return (
        AuditEvent.query.filter(
            AuditEvent.tenant_id == int(tenant_id),
            AuditEvent.resource_type == "tenant_jurisdiction",
            AuditEvent.resource_id == str(int(tenant_id)),
            AuditEvent.event_type.in_(_EVENT_TYPES),
        )
        .order_by(AuditEvent.id.asc())
        .all()
    )


def _event_for_idempotency(
    tenant_id: int,
    *,
    event_types: set[str],
    idempotency_key_sha256: str,
) -> AuditEvent | None:
    events = (
        AuditEvent.query.filter(
            AuditEvent.tenant_id == int(tenant_id),
            AuditEvent.resource_type == "tenant_jurisdiction",
            AuditEvent.resource_id == str(int(tenant_id)),
            AuditEvent.event_type.in_(event_types),
        )
        .order_by(AuditEvent.id.desc())
        .all()
    )
    return next(
        (
            event
            for event in events
            if _event_details(event).get("idempotency_key_sha256")
            == idempotency_key_sha256
        ),
        None,
    )


def _latest_submission_and_review(
    tenant_id: int,
) -> tuple[AuditEvent | None, AuditEvent | None]:
    events = _jurisdiction_events(tenant_id)
    submission = next(
        (event for event in reversed(events) if event.event_type == EVENT_EVIDENCE_SUBMITTED),
        None,
    )
    if submission is None:
        return None, None
    review = next(
        (
            event
            for event in reversed(events)
            if event.id > submission.id and event.event_type in {EVENT_VERIFIED, EVENT_REJECTED}
        ),
        None,
    )
    return submission, review


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _serialize_audit_event(event: AuditEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    details = _event_details(event)
    return {
        "id": int(event.id),
        "event_type": event.event_type,
        "actor_user_id": int(event.actor_user_id) if event.actor_user_id is not None else None,
        "decision": details.get("decision"),
        "submission_sha256": details.get("submission_sha256"),
        "reason_code": details.get("reason_code"),
        "created_at": _isoformat(event.created_at),
    }


def jurisdiction_readiness(tenant: TenantProfile) -> dict[str, Any]:
    """Return the fail-closed government publishing boundary for one tenant."""

    submission, review = _latest_submission_and_review(int(tenant.id))
    submission_details = _event_details(submission)
    review_details = _event_details(review)
    verified_ref = tenant_verified_jurisdiction(tenant)
    government_gate_required = tenant_requires_government_survey_evidence(tenant)
    configured_status = str(getattr(tenant, "jurisdiction_status", None) or "unverified").strip().lower()

    if configured_status == "verified" and verified_ref is None:
        state = "invalid_verified_record"
        next_action = "contact_platform_support"
    elif verified_ref is not None:
        state = "verified"
        next_action = "review_survey_content"
    elif review is not None and review.event_type == EVENT_REJECTED:
        state = "rejected"
        next_action = "resubmit_jurisdiction_evidence"
    elif submission is not None:
        state = "evidence_submitted"
        next_action = "await_platform_jurisdiction_review"
    elif configured_status == "not_applicable":
        state = "not_applicable"
        next_action = (
            "submit_jurisdiction_evidence"
            if government_gate_required
            else "continue_tenant_configuration"
        )
    else:
        state = "unverified"
        next_action = "submit_jurisdiction_evidence"

    allowed_to_publish = bool(not government_gate_required or verified_ref is not None)
    blocking_reason = (
        None if allowed_to_publish else "survey_tenant_jurisdiction_unverified"
    )
    submission_sha256 = submission_details.get("submission_sha256")
    evidence_sha256 = submission_details.get("evidence_sha256")
    review_matches_submission = bool(
        review is not None
        and submission_sha256
        and review_details.get("submission_sha256") == submission_sha256
    )

    return {
        "contract_version": READINESS_CONTRACT_VERSION,
        "tenant": {
            "id": int(tenant.id),
            "slug": tenant.slug,
            "type": tenant.tipo,
        },
        "state": state,
        "ready_to_publish": allowed_to_publish,
        "next_action": next_action,
        "publication_guard": {
            "government_evidence_required": government_gate_required,
            "allowed_to_publish": allowed_to_publish,
            "reason_code": blocking_reason,
            "guard_preserved": True,
        },
        "jurisdiction": {
            "status": configured_status,
            "reference": getattr(tenant, "jurisdiction_ref", None),
            "evidence": {
                "reference": getattr(tenant, "jurisdiction_evidence_ref", None),
                "document_sha256": evidence_sha256,
                "submission_sha256": submission_sha256,
                "raw_content_stored": False,
                "credentials_stored": False,
            },
            "verified_by_user_id": (
                int(tenant.jurisdiction_verified_by_user_id)
                if tenant.jurisdiction_verified_by_user_id is not None
                else None
            ),
            "verified_at": _isoformat(tenant.jurisdiction_verified_at),
        },
        "workflow": {
            "submission": _serialize_audit_event(submission),
            "review": _serialize_audit_event(review),
            "review_matches_submission": review_matches_submission,
            "separation_of_duties_enforced": True,
        },
    }


def submit_jurisdiction_evidence(
    tenant: TenantProfile,
    *,
    actor_user_id: int,
    jurisdiction_ref: Any,
    evidence_ref: Any,
    evidence_sha256: Any,
    idempotency_key: Any,
) -> tuple[dict[str, Any], bool]:
    """Stage one exact evidence submission without granting verification."""

    jurisdiction = _normalize_jurisdiction_ref(jurisdiction_ref)
    evidence = _normalize_evidence_ref(evidence_ref)
    document_sha256 = _normalize_sha256(evidence_sha256, field="evidence_sha256")
    key = _normalize_idempotency_key(idempotency_key)
    key_sha256 = _sha256(key)
    request_document = {
        "contract_version": SUBMISSION_CONTRACT_VERSION,
        "tenant_id": int(tenant.id),
        "jurisdiction_ref": jurisdiction,
        "evidence_ref": evidence,
        "evidence_sha256": document_sha256,
    }
    # Bind idempotency to the authenticated principal as well as the tenant and
    # payload. Another tenant administrator cannot replay somebody else's key
    # and inherit that actor's audit attribution.
    request_sha256 = _payload_digest(
        {**request_document, "actor_user_id": int(actor_user_id)}
    )

    locked_tenant = (
        TenantProfile.query.filter_by(id=int(tenant.id)).with_for_update().one_or_none()
    )
    if locked_tenant is None:
        raise GovernmentJurisdictionError(
            "tenant_not_found",
            "Tenant no encontrado.",
            404,
            next_action="check_tenant_selector",
        )

    existing = _event_for_idempotency(
        int(tenant.id),
        event_types={EVENT_EVIDENCE_SUBMITTED},
        idempotency_key_sha256=key_sha256,
    )
    if existing is not None:
        if _event_details(existing).get("request_sha256") != request_sha256:
            raise GovernmentJurisdictionError(
                "jurisdiction_idempotency_conflict",
                "Idempotency-Key ya fue usado con otra evidencia.",
                409,
                next_action="reuse_original_request_or_new_key",
            )
        return jurisdiction_readiness(locked_tenant), True

    submission_sha256 = request_sha256
    locked_tenant.jurisdiction_status = "unverified"
    locked_tenant.jurisdiction_ref = jurisdiction
    locked_tenant.jurisdiction_evidence_ref = evidence
    locked_tenant.jurisdiction_verified_by_user_id = None
    locked_tenant.jurisdiction_verified_at = None
    event = AuditEvent(
        tenant_id=int(locked_tenant.id),
        actor_user_id=int(actor_user_id),
        event_type=EVENT_EVIDENCE_SUBMITTED,
        resource_type="tenant_jurisdiction",
        resource_id=str(int(locked_tenant.id)),
        details={
            "contract_version": AUDIT_CONTRACT_VERSION,
            "decision": "submitted",
            "submission_sha256": submission_sha256,
            "request_sha256": request_sha256,
            "evidence_sha256": document_sha256,
            "evidence_ref_sha256": _sha256(evidence),
            "jurisdiction_ref_sha256": _sha256(jurisdiction),
            "idempotency_key_sha256": key_sha256,
            "raw_evidence_content_persisted": False,
            "credentials_persisted": False,
            "raw_references_persisted_in_audit": False,
        },
    )
    db.session.add(event)
    try:
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise GovernmentJurisdictionError(
            "jurisdiction_evidence_commit_failed",
            "No se pudo registrar atomicamente la evidencia.",
            503,
            next_action="retry_same_idempotency_key",
        ) from exc
    return jurisdiction_readiness(locked_tenant), False


def review_jurisdiction_evidence(
    tenant: TenantProfile,
    *,
    reviewer_user_id: int,
    decision: Any,
    expected_submission_sha256: Any,
    reason_code: Any = None,
    idempotency_key: Any,
    reviewed_at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Verify or reject the latest exact submission with separation of duties."""

    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"verify", "reject"}:
        raise GovernmentJurisdictionError(
            "jurisdiction_review_decision_invalid",
            "decision debe ser verify o reject.",
            400,
            next_action="send_verify_or_reject",
        )
    expected_submission = _normalize_sha256(
        expected_submission_sha256,
        field="expected_submission_sha256",
    )
    rejection_reason = (
        _normalize_rejection_reason(reason_code)
        if normalized_decision == "reject"
        else None
    )
    if normalized_decision == "verify" and reason_code not in (None, ""):
        raise GovernmentJurisdictionError(
            "jurisdiction_review_payload_invalid",
            "reason_code solo se admite al rechazar.",
            400,
            next_action="send_exact_review_payload",
        )
    key = _normalize_idempotency_key(idempotency_key)
    key_sha256 = _sha256(key)
    request_document = {
        "contract_version": REVIEW_CONTRACT_VERSION,
        "tenant_id": int(tenant.id),
        "decision": normalized_decision,
        "expected_submission_sha256": expected_submission,
        "reason_code": rejection_reason,
        "reviewer_user_id": int(reviewer_user_id),
    }
    request_sha256 = _payload_digest(request_document)

    locked_tenant = (
        TenantProfile.query.filter_by(id=int(tenant.id)).with_for_update().one_or_none()
    )
    if locked_tenant is None:
        raise GovernmentJurisdictionError(
            "tenant_not_found",
            "Tenant no encontrado.",
            404,
            next_action="check_tenant_selector",
        )

    existing = _event_for_idempotency(
        int(tenant.id),
        event_types={EVENT_VERIFIED, EVENT_REJECTED},
        idempotency_key_sha256=key_sha256,
    )
    if existing is not None:
        if _event_details(existing).get("request_sha256") != request_sha256:
            raise GovernmentJurisdictionError(
                "jurisdiction_idempotency_conflict",
                "Idempotency-Key ya fue usado con otra revision.",
                409,
                next_action="reuse_original_request_or_new_key",
            )
        return jurisdiction_readiness(locked_tenant), True

    submission, prior_review = _latest_submission_and_review(int(tenant.id))
    if submission is None:
        raise GovernmentJurisdictionError(
            "jurisdiction_evidence_not_submitted",
            "El tenant no tiene evidencia pendiente de revision.",
            409,
            next_action="submit_jurisdiction_evidence",
        )
    submission_details = _event_details(submission)
    current_submission = submission_details.get("submission_sha256")
    if current_submission != expected_submission:
        raise GovernmentJurisdictionError(
            "jurisdiction_submission_hash_conflict",
            "La evidencia cambio desde que fue cargada para revision.",
            409,
            next_action="reload_jurisdiction_readiness",
            details={"current_submission_sha256": current_submission},
        )
    if int(submission.actor_user_id or 0) == int(reviewer_user_id):
        raise GovernmentJurisdictionError(
            "jurisdiction_self_verification_forbidden",
            "Quien presento la evidencia no puede revisar su propia solicitud.",
            403,
            next_action="assign_independent_platform_reviewer",
        )
    if prior_review is not None:
        raise GovernmentJurisdictionError(
            "jurisdiction_submission_already_reviewed",
            "La evidencia ya tiene una decision registrada.",
            409,
            next_action=(
                "review_survey_content"
                if prior_review.event_type == EVENT_VERIFIED
                else "resubmit_jurisdiction_evidence"
            ),
        )

    jurisdiction = str(locked_tenant.jurisdiction_ref or "").strip()
    evidence = str(locked_tenant.jurisdiction_evidence_ref or "").strip()
    if (
        not _JURISDICTION_REF_RE.fullmatch(jurisdiction)
        or not evidence
        or _sha256(jurisdiction) != submission_details.get("jurisdiction_ref_sha256")
        or _sha256(evidence) != submission_details.get("evidence_ref_sha256")
    ):
        raise GovernmentJurisdictionError(
            "jurisdiction_submission_state_conflict",
            "La evidencia almacenada no coincide con la solicitud auditada.",
            409,
            next_action="resubmit_jurisdiction_evidence",
        )

    timestamp = reviewed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    if normalized_decision == "verify":
        locked_tenant.jurisdiction_status = "verified"
        locked_tenant.jurisdiction_verified_by_user_id = int(reviewer_user_id)
        locked_tenant.jurisdiction_verified_at = timestamp
        event_type = EVENT_VERIFIED
        audit_reason = "jurisdiction_evidence_verified"
    else:
        locked_tenant.jurisdiction_status = "unverified"
        locked_tenant.jurisdiction_verified_by_user_id = None
        locked_tenant.jurisdiction_verified_at = None
        event_type = EVENT_REJECTED
        audit_reason = rejection_reason

    event = AuditEvent(
        tenant_id=int(locked_tenant.id),
        actor_user_id=int(reviewer_user_id),
        event_type=event_type,
        resource_type="tenant_jurisdiction",
        resource_id=str(int(locked_tenant.id)),
        created_at=timestamp,
        details={
            "contract_version": AUDIT_CONTRACT_VERSION,
            "decision": normalized_decision,
            "submission_sha256": current_submission,
            "request_sha256": request_sha256,
            "evidence_sha256": submission_details.get("evidence_sha256"),
            "idempotency_key_sha256": key_sha256,
            "reason_code": audit_reason,
            "raw_evidence_content_persisted": False,
            "credentials_persisted": False,
            "raw_references_persisted_in_audit": False,
        },
    )
    db.session.add(event)
    try:
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise GovernmentJurisdictionError(
            "jurisdiction_review_commit_failed",
            "No se pudo registrar atomicamente la revision.",
            503,
            next_action="retry_same_idempotency_key",
        ) from exc
    return jurisdiction_readiness(locked_tenant), False


__all__ = [
    "GovernmentJurisdictionError",
    "jurisdiction_readiness",
    "review_jurisdiction_evidence",
    "submit_jurisdiction_evidence",
]
