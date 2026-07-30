"""Tenant-scoped assessment and interview persistence.

This module intentionally stops at human review. It contains no admission,
rejection, eligibility, hiring, or automated-decision field.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    event,
    select,
)

from database import db


PROGRAM_TYPES = (
    "school_admission",
    "municipal_intake",
    "employment_interview",
    "customer_qualification",
)
PROGRAM_STATUSES = ("draft", "active", "retired")
PROGRAM_VERSION_STATUSES = ("draft", "published")
ASSESSMENT_CASE_STATUSES = (
    "consent_pending",
    "scheduled",
    "in_progress",
    "awaiting_human_review",
    "withdrawn",
    "cancelled",
)
ASSESSMENT_SUBJECT_TYPES = ("student_applicant", "citizen", "candidate", "customer")
INTERVIEW_SESSION_STATUSES = (
    "scheduled",
    "active",
    "completed",
    "interrupted",
    "no_show",
    "void",
)
INTERVIEW_CHANNELS = ("api", "web", "widget", "whatsapp", "voice", "in_person")
INTERVIEW_CONSENT_SOURCES = ("web", "widget", "whatsapp", "voice", "in_person")
INTERVIEW_CONSENT_ATTESTATION_KINDS = (
    "participant_event",
    "operator_attestation",
    "legacy_unverified",
)
INTERVIEW_EVIDENCE_TYPES = (
    "audio",
    "image",
    "file",
    "transcript",
    "location",
    "structured",
)


def _utc_now():
    return datetime.now(timezone.utc)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class AssessmentProgram(db.Model):
    __tablename__ = "assessment_program"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(1000), nullable=True)
    program_type = db.Column(db.String(40), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default="draft", index=True)
    published_version_number = db.Column(db.Integer, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            f"program_type IN ({_sql_values(PROGRAM_TYPES)})",
            name="ck_assessment_program_type",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(PROGRAM_STATUSES)})",
            name="ck_assessment_program_status",
        ),
        CheckConstraint(
            "published_version_number IS NULL OR published_version_number > 0",
            name="ck_assessment_program_published_version",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_assessment_program_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assessment_program_tenant_idempotency",
        ),
    )


class AssessmentProgramVersion(db.Model):
    __tablename__ = "assessment_program_version"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    program_id = db.Column(db.Integer, nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="draft", index=True)
    definition_json = db.Column(db.JSON, nullable=False)
    definition_hash = db.Column(db.String(64), nullable=False)
    consent_policy_version = db.Column(db.String(64), nullable=False)
    consent_text = db.Column(db.Text, nullable=True)
    consent_text_sha256 = db.Column(db.String(64), nullable=False)
    consent_text_format = db.Column(db.String(24), nullable=True)
    consent_text_normalization = db.Column(db.String(48), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    create_idempotency_key = db.Column(db.String(128), nullable=False)
    create_request_hash = db.Column(db.String(64), nullable=False)
    published_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)
    publish_idempotency_key = db.Column(db.String(128), nullable=True)
    publish_request_hash = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "program_id"],
            ["assessment_program.tenant_id", "assessment_program.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "version_number > 0", name="ck_assessment_program_version_number"
        ),
        CheckConstraint(
            f"status IN ({_sql_values(PROGRAM_VERSION_STATUSES)})",
            name="ck_assessment_program_version_status",
        ),
        CheckConstraint(
            "(consent_text IS NULL AND consent_text_format IS NULL "
            "AND consent_text_normalization IS NULL) OR "
            "(consent_text IS NOT NULL AND consent_text_format = 'plain_text' "
            "AND consent_text_normalization = 'unicode_nfc_lf_trim_v1')",
            name="ck_assessment_program_version_consent_snapshot",
        ),
        CheckConstraint(
            "(status = 'draft' AND published_at IS NULL "
            "AND published_by_user_id IS NULL AND publish_idempotency_key IS NULL "
            "AND publish_request_hash IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL "
            "AND published_by_user_id IS NOT NULL AND publish_idempotency_key IS NOT NULL "
            "AND publish_request_hash IS NOT NULL)",
            name="ck_assessment_program_version_publish_state",
        ),
        UniqueConstraint(
            "tenant_id",
            "program_id",
            "version_number",
            name="uq_assessment_program_version_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "program_id",
            "id",
            name="uq_assessment_program_version_tenant_program_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "create_idempotency_key",
            name="uq_assessment_program_version_create_idem",
        ),
        UniqueConstraint(
            "tenant_id",
            "publish_idempotency_key",
            name="uq_assessment_program_version_publish_idem",
        ),
    )


@event.listens_for(AssessmentProgramVersion, "before_update")
def _published_program_version_is_immutable(_mapper, connection, target):
    """Block every ORM update after a version has been published once."""

    published_at = connection.execute(
        select(AssessmentProgramVersion.__table__.c.published_at).where(
            AssessmentProgramVersion.__table__.c.id == target.id
        )
    ).scalar_one_or_none()
    if published_at is not None:
        raise ValueError("published assessment program versions are immutable")


class AssessmentCase(db.Model):
    __tablename__ = "assessment_case"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    program_id = db.Column(db.Integer, nullable=False, index=True)
    program_version_id = db.Column(db.Integer, nullable=False, index=True)
    subject_type = db.Column(db.String(40), nullable=False)
    subject_ref = db.Column(db.String(160), nullable=False, index=True)
    status = db.Column(
        db.String(40), nullable=False, default="consent_pending", index=True
    )
    source_channel = db.Column(db.String(24), nullable=False)
    subject_identity_binding_id = db.Column(db.Integer, nullable=True, index=True)
    subject_identity_version = db.Column(db.String(32), nullable=True)
    subject_identity_hmac = db.Column(db.String(64), nullable=True)
    subject_chat_session_id = db.Column(db.String(36), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "program_id", "program_version_id"],
            [
                "assessment_program_version.tenant_id",
                "assessment_program_version.program_id",
                "assessment_program_version.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subject_identity_binding_id", "tenant_id"],
            [
                "channel_session_identity_binding.id",
                "channel_session_identity_binding.tenant_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"subject_type IN ({_sql_values(ASSESSMENT_SUBJECT_TYPES)})",
            name="ck_assessment_case_subject_type",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(ASSESSMENT_CASE_STATUSES)})",
            name="ck_assessment_case_status",
        ),
        CheckConstraint(
            f"source_channel IN ({_sql_values(INTERVIEW_CHANNELS)})",
            name="ck_assessment_case_source_channel",
        ),
        CheckConstraint(
            "(source_channel = 'whatsapp' "
            "AND subject_identity_binding_id IS NOT NULL "
            "AND subject_identity_version IS NOT NULL "
            "AND subject_identity_hmac IS NOT NULL "
            "AND length(subject_identity_hmac) = 64 "
            "AND subject_chat_session_id IS NOT NULL) OR "
            "(source_channel <> 'whatsapp' "
            "AND subject_identity_binding_id IS NULL "
            "AND subject_identity_version IS NULL "
            "AND subject_identity_hmac IS NULL "
            "AND subject_chat_session_id IS NULL)",
            name="ck_assessment_case_subject_channel_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "program_version_id",
            name="uq_assessment_case_tenant_id_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assessment_case_tenant_idempotency",
        ),
    )


class InterviewSession(db.Model):
    __tablename__ = "interview_session"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    assessment_case_id = db.Column(db.Integer, nullable=False, index=True)
    program_version_id = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default="scheduled", index=True)
    channel = db.Column(db.String(24), nullable=False)
    subject_identity_binding_id = db.Column(db.Integer, nullable=True, index=True)
    subject_identity_version = db.Column(db.String(32), nullable=True)
    subject_identity_hmac = db.Column(db.String(64), nullable=True)
    subject_chat_session_id = db.Column(db.String(36), nullable=True)
    interviewer_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    scheduled_for = db.Column(db.DateTime(timezone=True), nullable=True)
    consent_granted = db.Column(db.Boolean, nullable=False, default=False)
    consent_policy_version = db.Column(db.String(64), nullable=True)
    consent_text_sha256 = db.Column(db.String(64), nullable=True)
    consent_recorded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    consent_source = db.Column(db.String(24), nullable=True)
    consent_attestation_kind = db.Column(db.String(32), nullable=True)
    consent_evidence_provider = db.Column(db.String(64), nullable=True)
    consent_evidence_ref = db.Column(db.String(500), nullable=True)
    consent_evidence_sha256 = db.Column(db.String(64), nullable=True)
    consent_evidence_captured_at = db.Column(db.DateTime(timezone=True), nullable=True)
    consent_attested_by_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=True
    )
    create_idempotency_key = db.Column(db.String(128), nullable=False)
    create_request_hash = db.Column(db.String(64), nullable=False)
    start_idempotency_key = db.Column(db.String(128), nullable=True)
    start_request_hash = db.Column(db.String(64), nullable=True)
    complete_idempotency_key = db.Column(db.String(128), nullable=True)
    complete_request_hash = db.Column(db.String(64), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "assessment_case_id", "program_version_id"],
            [
                "assessment_case.tenant_id",
                "assessment_case.id",
                "assessment_case.program_version_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["subject_identity_binding_id", "tenant_id"],
            [
                "channel_session_identity_binding.id",
                "channel_session_identity_binding.tenant_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(INTERVIEW_SESSION_STATUSES)})",
            name="ck_interview_session_status",
        ),
        CheckConstraint(
            f"channel IN ({_sql_values(INTERVIEW_CHANNELS)})",
            name="ck_interview_session_channel",
        ),
        CheckConstraint(
            "(channel = 'whatsapp' "
            "AND subject_identity_binding_id IS NOT NULL "
            "AND subject_identity_version IS NOT NULL "
            "AND subject_identity_hmac IS NOT NULL "
            "AND length(subject_identity_hmac) = 64 "
            "AND subject_chat_session_id IS NOT NULL) OR "
            "(channel <> 'whatsapp' "
            "AND subject_identity_binding_id IS NULL "
            "AND subject_identity_version IS NULL "
            "AND subject_identity_hmac IS NULL "
            "AND subject_chat_session_id IS NULL)",
            name="ck_interview_session_subject_channel_identity",
        ),
        CheckConstraint(
            "(consent_granted = false AND consent_policy_version IS NULL "
            "AND consent_text_sha256 IS NULL "
            "AND consent_recorded_at IS NULL AND consent_source IS NULL "
            "AND consent_attestation_kind IS NULL "
            "AND consent_evidence_provider IS NULL "
            "AND consent_evidence_ref IS NULL "
            "AND consent_evidence_sha256 IS NULL "
            "AND consent_evidence_captured_at IS NULL "
            "AND consent_attested_by_user_id IS NULL) OR "
            "(consent_granted = true AND consent_policy_version IS NOT NULL "
            "AND consent_text_sha256 IS NOT NULL "
            "AND consent_recorded_at IS NOT NULL AND consent_source IS NOT NULL "
            "AND ((consent_attestation_kind = 'legacy_unverified' "
            "AND consent_evidence_provider IS NULL "
            "AND consent_evidence_ref IS NULL "
            "AND consent_evidence_sha256 IS NULL "
            "AND consent_evidence_captured_at IS NULL "
            "AND consent_attested_by_user_id IS NULL) OR "
            "(consent_attestation_kind = 'participant_event' "
            "AND consent_evidence_provider IS NOT NULL "
            "AND consent_evidence_ref IS NOT NULL "
            "AND consent_evidence_sha256 IS NOT NULL "
            "AND consent_evidence_captured_at IS NOT NULL "
            "AND consent_attested_by_user_id IS NOT NULL) OR "
            "(consent_attestation_kind = 'operator_attestation' "
            "AND consent_evidence_provider IS NULL "
            "AND consent_evidence_ref IS NULL "
            "AND consent_evidence_sha256 IS NULL "
            "AND consent_evidence_captured_at IS NOT NULL "
            "AND consent_attested_by_user_id IS NOT NULL)))",
            name="ck_interview_session_consent",
        ),
        CheckConstraint(
            "consent_source IS NULL OR "
            f"consent_source IN ({_sql_values(INTERVIEW_CONSENT_SOURCES)})",
            name="ck_interview_session_consent_source",
        ),
        CheckConstraint(
            "consent_attestation_kind IS NULL OR "
            f"consent_attestation_kind IN ({_sql_values(INTERVIEW_CONSENT_ATTESTATION_KINDS)})",
            name="ck_interview_session_consent_attestation_kind",
        ),
        CheckConstraint(
            "status NOT IN ('active', 'completed') OR consent_granted = true",
            name="ck_interview_session_active_consent",
        ),
        CheckConstraint(
            "(start_idempotency_key IS NULL AND start_request_hash IS NULL) OR "
            "(start_idempotency_key IS NOT NULL AND start_request_hash IS NOT NULL)",
            name="ck_interview_session_start_idempotency",
        ),
        CheckConstraint(
            "(complete_idempotency_key IS NULL AND complete_request_hash IS NULL) OR "
            "(complete_idempotency_key IS NOT NULL AND complete_request_hash IS NOT NULL)",
            name="ck_interview_session_complete_idempotency",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_interview_session_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "create_idempotency_key",
            name="uq_interview_session_create_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "start_idempotency_key",
            name="uq_interview_session_start_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "complete_idempotency_key",
            name="uq_interview_session_complete_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "consent_evidence_ref",
            name="uq_interview_session_consent_evidence_ref",
        ),
    )


class InterviewConsentChallenge(db.Model):
    """One-time, non-PII challenge binding WhatsApp consent to one identity."""

    __tablename__ = "interview_consent_challenge"

    CONTRACT_VERSION = "interview.consent_challenge.v3"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    interview_session_id = db.Column(db.Integer, nullable=False, index=True)
    program_version_id = db.Column(db.Integer, nullable=False, index=True)
    consent_text_sha256 = db.Column(db.String(64), nullable=False)
    expected_identity_binding_id = db.Column(db.Integer, nullable=False)
    expected_identity_version = db.Column(db.String(32), nullable=False)
    expected_identity_hmac = db.Column(db.String(64), nullable=False)
    expected_chat_session_id = db.Column(db.String(36), nullable=False)
    nonce_sha256 = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    issued_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    consumed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    consumed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    consumed_turn_id = db.Column(db.Integer, nullable=True)
    consumed_provider_message_sid = db.Column(db.String(180), nullable=True)
    contract_version = db.Column(
        db.String(48), nullable=False, default=CONTRACT_VERSION
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["expected_identity_binding_id", "tenant_id"],
            [
                "channel_session_identity_binding.id",
                "channel_session_identity_binding.tenant_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["consumed_turn_id", "tenant_id"],
            ["whatsapp_inbound_turn.id", "whatsapp_inbound_turn.tenant_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(consent_text_sha256) = 64",
            name="ck_interview_consent_challenge_text_hash",
        ),
        CheckConstraint(
            "length(expected_identity_hmac) = 64",
            name="ck_interview_consent_challenge_identity_hash",
        ),
        CheckConstraint(
            "length(expected_identity_version) > 0 "
            "AND length(expected_chat_session_id) > 0",
            name="ck_interview_consent_challenge_identity_snapshot",
        ),
        CheckConstraint(
            "length(nonce_sha256) = 64",
            name="ck_interview_consent_challenge_nonce_hash",
        ),
        CheckConstraint(
            "(consumed_at IS NULL AND consumed_by_user_id IS NULL "
            "AND consumed_turn_id IS NULL AND consumed_provider_message_sid IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_by_user_id IS NOT NULL "
            "AND consumed_turn_id IS NOT NULL AND consumed_provider_message_sid IS NOT NULL)",
            name="ck_interview_consent_challenge_consumption",
        ),
        UniqueConstraint(
            "tenant_id",
            "nonce_sha256",
            name="uq_interview_consent_challenge_tenant_nonce",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_interview_consent_challenge_tenant_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "consumed_provider_message_sid",
            name="uq_interview_consent_challenge_consumed_turn_sid",
        ),
    )


class InterviewConsentPresentation(db.Model):
    """Immutable proof that the exact consent payload reached the pinned channel."""

    __tablename__ = "interview_consent_presentation"

    CONTRACT_VERSION = "interview.consent_presentation.v1"
    ACTION_GRANT = "grant_consent"
    VERIFIED_PROVIDER_STATUSES = ("delivered", "read")

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    interview_session_id = db.Column(db.Integer, nullable=False, index=True)
    consent_challenge_id = db.Column(db.Integer, nullable=False)
    outbound_attempt_id = db.Column(db.String(36), nullable=False)
    outbound_provider = db.Column(db.String(32), nullable=False)
    outbound_provider_message_sid = db.Column(db.String(180), nullable=False)
    outbound_content_sid = db.Column(db.String(180), nullable=False)
    outbound_provider_status = db.Column(db.String(16), nullable=False)
    outbound_provider_status_at = db.Column(db.DateTime(timezone=True), nullable=False)
    outbound_payload_sha256 = db.Column(db.String(64), nullable=False)
    expected_identity_binding_id = db.Column(db.Integer, nullable=False)
    expected_identity_version = db.Column(db.String(32), nullable=False)
    expected_identity_hmac = db.Column(db.String(64), nullable=False)
    expected_chat_session_id = db.Column(db.String(36), nullable=False)
    consent_text_sha256 = db.Column(db.String(64), nullable=False)
    challenge_nonce_sha256 = db.Column(db.String(64), nullable=False)
    action = db.Column(db.String(32), nullable=False)
    registered_by_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False
    )
    registration_idempotency_key = db.Column(db.String(128), nullable=False)
    registration_request_hash = db.Column(db.String(64), nullable=False)
    registered_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    contract_version = db.Column(
        db.String(48), nullable=False, default=CONTRACT_VERSION
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "consent_challenge_id"],
            [
                "interview_consent_challenge.tenant_id",
                "interview_consent_challenge.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["expected_identity_binding_id", "tenant_id"],
            [
                "channel_session_identity_binding.id",
                "channel_session_identity_binding.tenant_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["outbound_attempt_id"],
            ["whatsapp_outbound_attempt.attempt_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "outbound_provider_status IN ('delivered', 'read')",
            name="ck_interview_consent_presentation_provider_status",
        ),
        CheckConstraint(
            "action = 'grant_consent'",
            name="ck_interview_consent_presentation_action",
        ),
        CheckConstraint(
            "length(outbound_payload_sha256) = 64 "
            "AND length(expected_identity_hmac) = 64 "
            "AND length(consent_text_sha256) = 64 "
            "AND length(challenge_nonce_sha256) = 64",
            name="ck_interview_consent_presentation_hashes",
        ),
        CheckConstraint(
            "outbound_provider_status_at <= registered_at",
            name="ck_interview_consent_presentation_ordering",
        ),
        UniqueConstraint(
            "consent_challenge_id",
            name="uq_interview_consent_presentation_challenge",
        ),
        UniqueConstraint(
            "tenant_id",
            "outbound_attempt_id",
            name="uq_interview_consent_presentation_attempt",
        ),
        UniqueConstraint(
            "tenant_id",
            "outbound_provider",
            "outbound_provider_message_sid",
            name="uq_interview_consent_presentation_provider_message",
        ),
        UniqueConstraint(
            "tenant_id",
            "registration_idempotency_key",
            name="uq_interview_consent_presentation_idempotency",
        ),
    )


@event.listens_for(InterviewConsentPresentation, "before_update")
@event.listens_for(InterviewConsentPresentation, "before_delete")
def _consent_presentation_is_immutable(_mapper, _connection, _target):
    raise ValueError("interview consent presentations are immutable")


class InterviewEvidence(db.Model):
    __tablename__ = "interview_evidence"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    interview_session_id = db.Column(db.Integer, nullable=False, index=True)
    evidence_type = db.Column(db.String(24), nullable=False)
    source_channel = db.Column(db.String(24), nullable=False)
    storage_ref = db.Column(db.String(500), nullable=False)
    content_sha256 = db.Column(db.String(64), nullable=False)
    provenance_json = db.Column(db.JSON, nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            f"evidence_type IN ({_sql_values(INTERVIEW_EVIDENCE_TYPES)})",
            name="ck_interview_evidence_type",
        ),
        CheckConstraint(
            f"source_channel IN ({_sql_values(INTERVIEW_CHANNELS)})",
            name="ck_interview_evidence_source_channel",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_interview_evidence_sha256_length",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_interview_evidence_size",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_interview_evidence_tenant_idempotency",
        ),
    )
