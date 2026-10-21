from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable

from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket
from models_territorial_geocoding import (
    TerritorialGeocodingAttempt,
    TerritorialGeocodingJob,
)
from services.tenant_ticket_scope import municipio_ticket_belongs_to_tenant
from services.territorial_evidence import extract_location_evidence, normalize_address_key
from services.territorial_geocoding import TerritorialGeocodingCandidate


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SQLAlchemyTerritorialGeocodingAuditStore:
    """Durable audit repository; transaction ownership stays with the caller."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def find_attempt(
        self,
        candidate_fingerprint: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        attempt = (
            self.session.query(TerritorialGeocodingAttempt).join(
                TerritorialGeocodingJob,
                TerritorialGeocodingJob.id == TerritorialGeocodingAttempt.job_id,
            )
            .filter(
                TerritorialGeocodingJob.candidate_fingerprint
                == candidate_fingerprint,
                TerritorialGeocodingAttempt.request_digest == request_digest,
            )
            .one_or_none()
        )
        if attempt is None or not isinstance(attempt.result_json, dict):
            return None
        return _json_copy(attempt.result_json)

    def record_attempt(
        self,
        candidate: TerritorialGeocodingCandidate,
        request_digest: str,
        outcome: dict[str, Any],
        *,
        external_call_performed: bool,
        write_performed: bool,
    ) -> TerritorialGeocodingAttempt:
        now = datetime.now(timezone.utc)
        job = (
            self.session.query(TerritorialGeocodingJob).filter_by(
                tenant_id=candidate.tenant_id,
                candidate_fingerprint=candidate.fingerprint,
            ).one_or_none()
        )
        if job is None:
            job = TerritorialGeocodingJob(
                tenant_id=candidate.tenant_id,
                contract_version=str(outcome.get("contract_version") or ""),
                source_model=candidate.record_source,
                source_id=candidate.record_id,
                candidate_fingerprint=candidate.fingerprint,
                address_digest=candidate.address_digest,
                jurisdiction_digest=candidate.jurisdiction_digest,
                status="pending",
                reason_code="candidate_discovered",
                validation_json={},
                result_json={},
                attempt_count=0,
            )
            self.session.add(job)
            self.session.flush()

        existing = self.session.query(TerritorialGeocodingAttempt).filter_by(
            job_id=job.id,
            request_digest=request_digest,
        ).one_or_none()
        if existing is not None:
            return existing

        safe_outcome = _json_copy(outcome)
        proposal = safe_outcome.get("proposal") if isinstance(safe_outcome.get("proposal"), dict) else {}
        validation = safe_outcome.get("validation") if isinstance(safe_outcome.get("validation"), dict) else {}
        execution = safe_outcome.get("execution") if isinstance(safe_outcome.get("execution"), dict) else {}
        action = str(execution.get("action") or "").strip().lower() or None
        idempotency_key_hash = (
            str(execution.get("idempotency_key_hash") or "").strip().lower()
            or None
        )
        if action not in {"resolve", "apply"}:
            action = None
            idempotency_key_hash = None
        if idempotency_key_hash is not None and len(idempotency_key_hash) != 64:
            raise ValueError("territorial geocoding idempotency hash invalid")
        status = str(safe_outcome.get("status") or "failed")
        attempt_number = int(job.attempt_count or 0) + 1
        attempt = TerritorialGeocodingAttempt(
            job_id=job.id,
            tenant_id=candidate.tenant_id,
            attempt_number=attempt_number,
            request_digest=request_digest,
            action=action,
            idempotency_key_hash=idempotency_key_hash,
            provider=str(safe_outcome.get("provider") or "").strip() or None,
            outcome_status=status,
            reason_code=str(safe_outcome.get("reason_code") or "unknown"),
            external_call_performed=bool(external_call_performed),
            write_performed=bool(write_performed),
            result_digest=_digest(safe_outcome),
            result_json=safe_outcome,
            created_at=now,
        )
        self.session.add(attempt)

        # Once applied, later diagnostic runs cannot downgrade the durable job.
        if job.status != "applied" or status == "applied":
            job.status = status
            job.reason_code = str(safe_outcome.get("reason_code") or "unknown")
            job.provider = str(safe_outcome.get("provider") or "").strip() or None
            job.provider_place_id = str(proposal.get("place_id") or "").strip() or None
            job.proposed_lat = proposal.get("lat")
            job.proposed_lng = proposal.get("lng")
            job.location_type = str(proposal.get("location_type") or "").strip() or None
            job.partial_match = (
                bool(proposal.get("partial_match")) if proposal else None
            )
            job.validation_json = _json_copy(validation)
            job.result_json = safe_outcome
            if status == "applied":
                job.applied_at = now
        job.attempt_count = attempt_number
        job.last_attempt_at = now
        self.session.flush()
        return attempt


def _persisted_address(record_source: str, record: Any) -> str | None:
    if record_source == "tenant_ticket":
        location = extract_location_evidence(
            ("ticket_metadata", getattr(record, "datos_extra", None) or {}),
        )
        return location.get("address")
    return getattr(record, "direccion", None)


def build_ticket_coordinate_applier(
    session: Any,
    *,
    locked_source_record: Any | None = None,
) -> Callable[[TerritorialGeocodingCandidate, dict[str, Any]], bool]:
    """Build the explicit, tenant-scoped callback required by the write gate."""

    model_by_source = {
        "tenant_ticket": TenantTicket,
        "municipio_ticket": MunicipioTicket,
        "pyme_ticket": PymeTicket,
    }

    def apply(
        candidate: TerritorialGeocodingCandidate,
        proposal: dict[str, Any],
    ) -> bool:
        model = model_by_source.get(candidate.record_source)
        if model is None:
            return False
        try:
            record_id = int(candidate.record_id)
            lat = float(proposal["lat"])
            lng = float(proposal["lng"])
        except (KeyError, TypeError, ValueError):
            return False
        record = locked_source_record
        if record is not None and (
            not isinstance(record, model) or int(getattr(record, "id", -1)) != record_id
        ):
            return False
        tenant_scoped_record = record is not None
        if record is None:
            record = session.query(model).filter_by(
                id=record_id,
                tenant_id=candidate.tenant_id,
            ).with_for_update().one_or_none()
            tenant_scoped_record = record is not None
        if record is None and candidate.record_source == "municipio_ticket":
            legacy_record = (
                session.query(model).filter_by(id=record_id).with_for_update().one_or_none()
            )
            tenant = session.query(TenantProfile).filter_by(
                id=candidate.tenant_id
            ).one_or_none()
            record = (
                legacy_record
                if tenant is not None
                and municipio_ticket_belongs_to_tenant(legacy_record, tenant)
                else None
            )
        if record is None:
            return False
        if (
            getattr(record, "tenant_id", None) != candidate.tenant_id
            and candidate.record_source != "municipio_ticket"
        ):
            return False
        if candidate.record_source == "municipio_ticket" and not tenant_scoped_record:
            tenant = session.query(TenantProfile).filter_by(
                id=candidate.tenant_id
            ).one_or_none()
            if tenant is None or not municipio_ticket_belongs_to_tenant(record, tenant):
                return False

        # Reject stale jobs if the operator corrected the address after discovery.
        current_address = normalize_address_key(
            _persisted_address(candidate.record_source, record)
        )
        if not current_address or current_address != candidate.normalized_address:
            return False

        current_lat = getattr(record, "latitud", None)
        current_lng = getattr(record, "longitud", None)
        if current_lat is not None or current_lng is not None:
            # Replays are handled by the durable attempt receipt before this
            # callback. Reaching a populated source row here means another
            # writer won the race or provenance is unknown; never overwrite or
            # silently accept it.
            return False

        existing_metadata = getattr(record, "datos_extra", None)
        if (
            isinstance(existing_metadata, dict)
            and existing_metadata.get("territorial_coordinate_provenance")
            is not None
        ):
            return False

        record.latitud = lat
        record.longitud = lng
        if hasattr(record, "datos_extra"):
            metadata = (
                dict(getattr(record, "datos_extra", None))
                if isinstance(getattr(record, "datos_extra", None), dict)
                else {}
            )
            provenance = proposal.get("provenance")
            metadata["territorial_coordinate_provenance"] = {
                "contract_version": "operations.coordinate_provenance.v1",
                "coordinate_reference": "WGS84",
                "source": "approved_geocoding_proposal",
                "provider": (
                    provenance.get("provider")
                    if isinstance(provenance, dict)
                    else None
                ),
                "proposal_digest": (
                    provenance.get("proposal_digest")
                    if isinstance(provenance, dict)
                    else None
                ),
            }
            record.datos_extra = metadata
        session.flush()
        return True

    return apply


__all__ = [
    "SQLAlchemyTerritorialGeocodingAuditStore",
    "build_ticket_coordinate_applier",
]
