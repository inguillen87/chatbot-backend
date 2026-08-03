"""Opaque, tenant-scoped eligibility grants for controlled survey intake.

The grant ledger intentionally stores no raw subject identifier and no plaintext
credential.  It provides pseudonymous internal linkability, not ballot secrecy
or an electoral certification.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, event, select

from database import db


SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION = "surveys.eligibility_grant.v1"
SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION = (
    "surveys.eligibility_redemption.v1"
)
SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION = "surveys.public_eligibility.v1"
SURVEY_ELIGIBILITY_KEY_VERSION = "v1"
SURVEY_ELIGIBILITY_MODES = ("institution_attested", "manual_review")
SURVEY_ELIGIBILITY_REVOCATION_REASONS = (
    "administrative_revocation",
    "subject_ineligible",
    "credential_compromised",
    "duplicate_issue",
    "other_reviewed",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SurveyEligibilityGrant(db.Model):
    """Immutable issuance receipt for one opaque, high-entropy credential."""

    __tablename__ = "survey_eligibility_grant"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    survey_id = db.Column(db.Integer, nullable=False)
    release_id = db.Column(db.Integer, nullable=False)
    grant_ref = db.Column(db.String(64), nullable=False)
    eligibility_policy_version = db.Column(db.String(64), nullable=False)
    eligibility_mode = db.Column(db.String(32), nullable=False)
    subject_namespace = db.Column(db.String(64), nullable=False)
    subject_hmac = db.Column(db.String(64), nullable=False)
    generation = db.Column(db.Integer, nullable=False)
    subject_key_version = db.Column(db.String(16), nullable=False)
    authority_namespace = db.Column(db.String(64), nullable=False)
    authority_adapter_version = db.Column(db.String(64), nullable=False)
    review_reference_hmac = db.Column(db.String(64), nullable=False)
    review_key_version = db.Column(db.String(16), nullable=False)
    credential_digest = db.Column(db.String(64), nullable=False)
    credential_key_version = db.Column(db.String(16), nullable=False)
    issued_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    issue_idempotency_key = db.Column(db.String(128), nullable=False)
    issue_request_hash = db.Column(db.String(64), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "survey_id",
                "release_id",
                "eligibility_policy_version",
            ],
            [
                "survey_governance_release.tenant_id",
                "survey_governance_release.survey_id",
                "survey_governance_release.id",
                "survey_governance_release.eligibility_policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_survey_eligibility_grant_release_scope",
        ),
        CheckConstraint(
            "eligibility_mode IN ('institution_attested', 'manual_review')",
            name="ck_survey_eligibility_grant_mode",
        ),
        CheckConstraint(
            "length(subject_namespace) BETWEEN 2 AND 64 "
            "AND subject_namespace = authority_namespace "
            "AND length(subject_hmac) = 64",
            name="ck_survey_eligibility_grant_subject_hmac",
        ),
        CheckConstraint(
            "length(grant_ref) = 48 AND substr(grant_ref, 1, 5) = 'seg1_'",
            name="ck_survey_eligibility_grant_ref_format",
        ),
        CheckConstraint(
            "length(subject_key_version) BETWEEN 2 AND 16 "
            "AND length(review_key_version) BETWEEN 2 AND 16 "
            "AND length(credential_key_version) BETWEEN 2 AND 16",
            name="ck_survey_eligibility_grant_key_versions",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_survey_eligibility_grant_generation",
        ),
        CheckConstraint(
            "length(authority_namespace) BETWEEN 2 AND 64 "
            "AND length(authority_adapter_version) BETWEEN 1 AND 64",
            name="ck_survey_eligibility_grant_authority",
        ),
        CheckConstraint(
            "length(review_reference_hmac) = 64",
            name="ck_survey_eligibility_grant_review_hmac",
        ),
        CheckConstraint(
            "length(credential_digest) = 64",
            name="ck_survey_eligibility_grant_credential_digest",
        ),
        CheckConstraint(
            "length(issue_request_hash) = 64",
            name="ck_survey_eligibility_grant_request_hash",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_survey_eligibility_grant_expiry",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_survey_eligibility_grant_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "survey_id",
            "release_id",
            "id",
            name="uq_survey_eligibility_grant_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "survey_id",
            "release_id",
            "id",
            "eligibility_policy_version",
            name="uq_survey_eligibility_grant_scope_policy_id",
        ),
        UniqueConstraint(
            "tenant_id", "grant_ref", name="uq_survey_eligibility_grant_ref"
        ),
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "subject_hmac",
            "generation",
            name="uq_survey_eligibility_grant_subject",
        ),
        UniqueConstraint(
            "tenant_id",
            "release_id",
            "credential_digest",
            name="uq_survey_eligibility_grant_credential",
        ),
        UniqueConstraint(
            "credential_key_version",
            "credential_digest",
            name="uq_survey_eligibility_grant_key_digest",
        ),
        UniqueConstraint(
            "tenant_id",
            "issue_idempotency_key",
            name="uq_survey_eligibility_grant_issue_idem",
        ),
        db.Index(
            "ix_survey_eligibility_grant_scope_status",
            "tenant_id",
            "survey_id",
            "release_id",
            "expires_at",
        ),
    )


class SurveyEligibilityTerminal(db.Model):
    """One append-only terminal event: either revocation or redemption.

    A single unique row per grant makes revoke-vs-redeem mutual exclusion a
    database invariant, including on SQLite where row locks are limited.
    """

    __tablename__ = "survey_eligibility_terminal"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    survey_id = db.Column(db.Integer, nullable=False)
    release_id = db.Column(db.Integer, nullable=False)
    grant_id = db.Column(db.Integer, nullable=False)
    disposition = db.Column(db.String(16), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    reason_code = db.Column(db.String(40), nullable=True)
    idempotency_key = db.Column(db.String(128), nullable=True)
    response_id = db.Column(db.Integer, nullable=True)
    eligibility_policy_version = db.Column(db.String(64), nullable=False)
    submission_payload_hash = db.Column(db.String(64), nullable=True)
    request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "survey_id",
                "release_id",
                "grant_id",
                "eligibility_policy_version",
            ],
            [
                "survey_eligibility_grant.tenant_id",
                "survey_eligibility_grant.survey_id",
                "survey_eligibility_grant.release_id",
                "survey_eligibility_grant.id",
                "survey_eligibility_grant.eligibility_policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_survey_eligibility_terminal_grant_scope",
        ),
        CheckConstraint(
            "(disposition = 'revoked' AND actor_user_id IS NOT NULL "
            "AND reason_code IS NOT NULL "
            "AND reason_code IN ('administrative_revocation', 'subject_ineligible', "
            "'credential_compromised', 'duplicate_issue', 'other_reviewed') "
            "AND idempotency_key IS NOT NULL AND response_id IS NULL "
            "AND submission_payload_hash IS NULL) OR "
            "(disposition = 'redeemed' AND actor_user_id IS NULL "
            "AND reason_code IS NULL AND idempotency_key IS NULL "
            "AND response_id IS NOT NULL AND submission_payload_hash IS NOT NULL "
            "AND length(submission_payload_hash) = 64)",
            name="ck_survey_eligibility_terminal_disposition",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_survey_eligibility_terminal_request_hash",
        ),
        UniqueConstraint(
            "grant_id", name="uq_survey_eligibility_terminal_grant"
        ),
        UniqueConstraint(
            "response_id", name="uq_survey_eligibility_terminal_response"
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_eligibility_terminal_idem",
        ),
        db.Index(
            "ix_survey_eligibility_terminal_scope",
            "tenant_id",
            "survey_id",
            "release_id",
            "disposition",
            "created_at",
        ),
    )


_IMMUTABLE_MODELS = (
    SurveyEligibilityGrant,
    SurveyEligibilityTerminal,
)


def _immutable_update(_mapper, _connection, _target) -> None:
    raise ValueError("survey eligibility history is immutable")


def _immutable_delete(_mapper, _connection, _target) -> None:
    raise ValueError("survey eligibility history is immutable")


for _model in _IMMUTABLE_MODELS:
    event.listen(_model, "before_update", _immutable_update)
    event.listen(_model, "before_delete", _immutable_delete)


@event.listens_for(SurveyEligibilityTerminal, "before_insert")
def _validate_redemption_response_scope(_mapper, connection, target) -> None:
    """Validate the response scope without retaining an on-delete FK.

    Source-anonymous retention may later remove the ballot row.  The minimal
    redemption receipt remains append-only and carries no answer content.
    """

    if target.disposition != "redeemed":
        return
    response_table = db.metadata.tables.get("enc_respuesta")
    grant_table = db.metadata.tables.get("survey_eligibility_grant")
    if response_table is None or grant_table is None:
        raise ValueError("survey eligibility scope tables are unavailable")
    row = connection.execute(
        select(
            response_table.c.tenant_id,
            response_table.c.encuesta_id,
            response_table.c.governance_release_id,
            response_table.c.governance_eligibility_policy_version,
            response_table.c.eligibility_contract_version,
            response_table.c.eligibility_decision,
            response_table.c.eligibility_verified_at,
        ).where(response_table.c.id == target.response_id)
    ).one_or_none()
    grant_row = connection.execute(
        select(grant_table.c.issued_at, grant_table.c.expires_at).where(
            grant_table.c.id == target.grant_id,
            grant_table.c.tenant_id == target.tenant_id,
            grant_table.c.survey_id == target.survey_id,
            grant_table.c.release_id == target.release_id,
            grant_table.c.eligibility_policy_version
            == target.eligibility_policy_version,
        )
    ).one_or_none()
    valid = (
        row is not None
        and grant_row is not None
        and tuple(row[:3])
        == (target.tenant_id, target.survey_id, target.release_id)
        and row.governance_eligibility_policy_version
        == target.eligibility_policy_version
        and row.eligibility_contract_version
        == SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION
        and row.eligibility_decision == "verified_by_opaque_grant"
        and row.eligibility_verified_at is not None
        and target.created_at is not None
        and _as_utc(row.eligibility_verified_at) == _as_utc(target.created_at)
        and _as_utc(grant_row.issued_at) <= _as_utc(target.created_at)
        and _as_utc(target.created_at) < _as_utc(grant_row.expires_at)
    )
    if not valid:
        raise ValueError("survey eligibility redemption response scope mismatch")


__all__ = [
    "SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION",
    "SURVEY_ELIGIBILITY_KEY_VERSION",
    "SURVEY_ELIGIBILITY_MODES",
    "SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION",
    "SURVEY_ELIGIBILITY_REVOCATION_REASONS",
    "SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION",
    "SurveyEligibilityGrant",
    "SurveyEligibilityTerminal",
]
