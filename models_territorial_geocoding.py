from __future__ import annotations

import uuid

from models import JSONType, TimestampMixin, db


class TerritorialGeocodingJob(db.Model, TimestampMixin):
    """Durable tenant-scoped work item without a copied street address."""

    __tablename__ = "territorial_geocoding_job"

    CONTRACT_VERSION = "operations.territorial_geocoding.v1"
    VALID_STATUSES = frozenset({"pending", "needs_review", "applied", "failed"})

    id = db.Column(
        db.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contract_version = db.Column(
        db.String(64), nullable=False, default=CONTRACT_VERSION
    )
    source_model = db.Column(db.String(32), nullable=False)
    source_id = db.Column(db.String(64), nullable=False)
    candidate_fingerprint = db.Column(db.String(64), nullable=False)
    address_digest = db.Column(db.String(64), nullable=False)
    jurisdiction_digest = db.Column(db.String(64), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default="pending", server_default="pending"
    )
    reason_code = db.Column(db.String(96), nullable=False)
    provider = db.Column(db.String(32), nullable=True)
    provider_place_id = db.Column(db.String(255), nullable=True)
    proposed_lat = db.Column(db.Float, nullable=True)
    proposed_lng = db.Column(db.Float, nullable=True)
    location_type = db.Column(db.String(32), nullable=True)
    partial_match = db.Column(db.Boolean, nullable=True)
    validation_json = db.Column(JSONType, nullable=False, default=dict)
    result_json = db.Column(JSONType, nullable=False, default=dict)
    attempt_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    last_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True)
    applied_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id",
            "candidate_fingerprint",
            name="uq_territorial_geocoding_job_candidate",
        ),
        db.CheckConstraint(
            "status IN ('pending', 'needs_review', 'applied', 'failed')",
            name="ck_territorial_geocoding_job_status",
        ),
        db.CheckConstraint(
            "attempt_count >= 0",
            name="ck_territorial_geocoding_job_attempt_count",
        ),
        db.CheckConstraint(
            "length(candidate_fingerprint) = 64",
            name="ck_territorial_geocoding_job_fingerprint",
        ),
        db.CheckConstraint(
            "length(address_digest) = 64",
            name="ck_territorial_geocoding_job_address_digest",
        ),
        db.CheckConstraint(
            "length(jurisdiction_digest) = 64",
            name="ck_territorial_geocoding_job_jurisdiction_digest",
        ),
        db.Index(
            "ix_territorial_geocoding_job_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
            "id",
        ),
    )


class TerritorialGeocodingAttempt(db.Model):
    """Immutable provider/write attempt receipt linked to one durable job."""

    __tablename__ = "territorial_geocoding_attempt"

    id = db.Column(
        db.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    job_id = db.Column(
        db.String(36),
        db.ForeignKey("territorial_geocoding_job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number = db.Column(db.Integer, nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    provider = db.Column(db.String(32), nullable=True)
    outcome_status = db.Column(db.String(20), nullable=False)
    reason_code = db.Column(db.String(96), nullable=False)
    external_call_performed = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )
    write_performed = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )
    result_digest = db.Column(db.String(64), nullable=False)
    result_json = db.Column(JSONType, nullable=False, default=dict)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    __table_args__ = (
        db.UniqueConstraint(
            "job_id",
            "request_digest",
            name="uq_territorial_geocoding_attempt_request",
        ),
        db.UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_territorial_geocoding_attempt_number",
        ),
        db.CheckConstraint(
            "outcome_status IN ('pending', 'needs_review', 'applied', 'failed')",
            name="ck_territorial_geocoding_attempt_status",
        ),
        db.CheckConstraint(
            "attempt_number > 0",
            name="ck_territorial_geocoding_attempt_number",
        ),
        db.CheckConstraint(
            "length(request_digest) = 64",
            name="ck_territorial_geocoding_attempt_request_digest",
        ),
        db.CheckConstraint(
            "length(result_digest) = 64",
            name="ck_territorial_geocoding_attempt_result_digest",
        ),
        db.CheckConstraint(
            "write_performed IS FALSE OR outcome_status = 'applied'",
            name="ck_territorial_geocoding_attempt_write_state",
        ),
        db.Index(
            "ix_territorial_geocoding_attempt_tenant_created",
            "tenant_id",
            "created_at",
            "id",
        ),
    )


__all__ = ["TerritorialGeocodingAttempt", "TerritorialGeocodingJob"]
