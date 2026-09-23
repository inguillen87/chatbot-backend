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
    action = db.Column(db.String(16), nullable=True)
    idempotency_key_hash = db.Column(db.String(64), nullable=True)
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
        db.UniqueConstraint(
            "job_id",
            "action",
            "idempotency_key_hash",
            name="uq_territorial_geocoding_attempt_idempotency",
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
            "(action IS NULL AND idempotency_key_hash IS NULL) OR "
            "(action IN ('resolve', 'apply') AND length(idempotency_key_hash) = 64)",
            name="ck_territorial_geocoding_attempt_idempotency",
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


class TerritorialGeocodingReview(db.Model):
    """Immutable human decision over one geocoding proposal.

    A review never stores the source address and never applies coordinates.  It
    binds the operator decision to the proposal digest that was visible at the
    time, so a later provider attempt makes the older decision observably stale
    instead of silently reusing it for different coordinates.
    """

    __tablename__ = "territorial_geocoding_review"

    CONTRACT_VERSION = "operations.territorial_geocoding_admin.v1"
    VALID_DECISIONS = frozenset({"approved", "rejected"})

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
    reviewer_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    contract_version = db.Column(
        db.String(64), nullable=False, default=CONTRACT_VERSION
    )
    decision = db.Column(db.String(16), nullable=False)
    reason_code = db.Column(db.String(96), nullable=False)
    reviewed_job_status = db.Column(db.String(20), nullable=False)
    proposal_digest = db.Column(db.String(64), nullable=False)
    proposal_attempt_id = db.Column(
        db.String(36),
        db.ForeignKey("territorial_geocoding_attempt.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    proposal_attempt_number = db.Column(db.Integer, nullable=True)
    idempotency_key_hash = db.Column(db.String(64), nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    coordinate_write_performed = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id",
            "job_id",
            "idempotency_key_hash",
            name="uq_territorial_geocoding_review_idempotency",
        ),
        db.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_territorial_geocoding_review_decision",
        ),
        db.CheckConstraint(
            "reviewed_job_status IN ('pending', 'needs_review', 'failed')",
            name="ck_territorial_geocoding_review_job_status",
        ),
        db.CheckConstraint(
            "length(proposal_digest) = 64 AND "
            "length(idempotency_key_hash) = 64 AND "
            "length(request_digest) = 64",
            name="ck_territorial_geocoding_review_digests",
        ),
        db.CheckConstraint(
            "(proposal_attempt_id IS NULL AND proposal_attempt_number IS NULL) OR "
            "(length(proposal_attempt_id) = 36 AND proposal_attempt_number > 0)",
            name="ck_territorial_geocoding_review_proposal_attempt",
        ),
        db.CheckConstraint(
            "coordinate_write_performed IS FALSE",
            name="ck_territorial_geocoding_review_no_coordinate_write",
        ),
        db.Index(
            "ix_territorial_geocoding_review_tenant_job_created",
            "tenant_id",
            "job_id",
            "created_at",
            "id",
        ),
    )


class TerritorialGeocodingSyncReceipt(db.Model, TimestampMixin):
    """Idempotency receipt for explicit queue materialization.

    Only hashes and the already-redacted aggregate response are persisted.
    Source addresses and candidate coordinates never cross this boundary.
    """

    __tablename__ = "territorial_geocoding_sync_receipt"

    CONTRACT_VERSION = "operations.territorial_geocoding_sync.v1"

    id = db.Column(
        db.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    contract_version = db.Column(
        db.String(64), nullable=False, default=CONTRACT_VERSION
    )
    idempotency_key_hash = db.Column(db.String(64), nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    completed = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )
    response_json = db.Column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_territorial_geocoding_sync_idempotency",
        ),
        db.CheckConstraint(
            "length(idempotency_key_hash) = 64 AND length(request_digest) = 64",
            name="ck_territorial_geocoding_sync_digests",
        ),
        db.Index(
            "ix_territorial_geocoding_sync_tenant_created",
            "tenant_id",
            "created_at",
            "id",
        ),
    )


__all__ = [
    "TerritorialGeocodingAttempt",
    "TerritorialGeocodingJob",
    "TerritorialGeocodingReview",
    "TerritorialGeocodingSyncReceipt",
]
