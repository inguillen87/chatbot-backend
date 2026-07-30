"""Immutable, tenant-scoped governance releases for surveys and votes.

The contract is deliberately not an electoral certification system. Quorum,
tie and challenge rules are declarative inputs for human review only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, event, select

from database import db


SURVEY_RELEASE_STATUSES = ("draft", "published", "closed")
SURVEY_RELEASE_CONTRACT_VERSION = "surveys.governance_release.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SurveyGovernanceRelease(db.Model):
    """One immutable instrument/policy snapshot and its lifecycle receipt."""

    __tablename__ = "survey_governance_release"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    survey_id = db.Column(db.Integer, nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    contract_version = db.Column(
        db.String(48), nullable=False, default=SURVEY_RELEASE_CONTRACT_VERSION
    )

    # Canonical JSON is stored as text so the exact hashed bytes are portable
    # across SQLite/PostgreSQL and can be compared by DB triggers.
    snapshot_json = db.Column(db.Text, nullable=False)
    snapshot_sha256 = db.Column(db.String(64), nullable=False)
    policy_sha256 = db.Column(db.String(64), nullable=False)
    eligibility_policy_version = db.Column(db.String(64), nullable=False)
    consent_policy_version = db.Column(db.String(64), nullable=False)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    create_idempotency_key = db.Column(db.String(128), nullable=False)
    create_request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)

    published_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)
    publish_idempotency_key = db.Column(db.String(128), nullable=True)
    publish_request_hash = db.Column(db.String(64), nullable=True)

    closed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    close_idempotency_key = db.Column(db.String(128), nullable=True)
    close_request_hash = db.Column(db.String(64), nullable=True)
    closure_manifest_json = db.Column(db.Text, nullable=True)
    closure_manifest_sha256 = db.Column(db.String(64), nullable=True)
    closed_response_count = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "survey_id"],
            ["enc_encuesta.tenant_id", "enc_encuesta.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'closed')",
            name="ck_survey_governance_release_status",
        ),
        CheckConstraint(
            "version_number > 0",
            name="ck_survey_governance_release_version",
        ),
        CheckConstraint(
            "closed_response_count IS NULL OR closed_response_count >= 0",
            name="ck_survey_governance_release_response_count",
        ),
        CheckConstraint(
            "(status = 'draft' AND published_at IS NULL AND closed_at IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL AND closed_at IS NULL "
            "AND published_by_user_id IS NOT NULL AND publish_idempotency_key IS NOT NULL "
            "AND publish_request_hash IS NOT NULL) OR "
            "(status = 'closed' AND published_at IS NOT NULL AND closed_at IS NOT NULL "
            "AND published_by_user_id IS NOT NULL AND publish_idempotency_key IS NOT NULL "
            "AND publish_request_hash IS NOT NULL AND closed_by_user_id IS NOT NULL "
            "AND close_idempotency_key IS NOT NULL AND close_request_hash IS NOT NULL "
            "AND closure_manifest_json IS NOT NULL AND closure_manifest_sha256 IS NOT NULL "
            "AND closed_response_count IS NOT NULL)",
            name="ck_survey_governance_release_lifecycle",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_survey_governance_release_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "survey_id",
            "version_number",
            name="uq_survey_governance_release_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "create_idempotency_key",
            name="uq_survey_governance_release_create_idem",
        ),
        UniqueConstraint(
            "tenant_id",
            "publish_idempotency_key",
            name="uq_survey_governance_release_publish_idem",
        ),
        UniqueConstraint(
            "tenant_id",
            "close_idempotency_key",
            name="uq_survey_governance_release_close_idem",
        ),
        db.Index(
            "uq_survey_governance_one_published",
            "tenant_id",
            "survey_id",
            unique=True,
            sqlite_where=db.text("status = 'published'"),
            postgresql_where=db.text("status = 'published'"),
        ),
    )


_IMMUTABLE_RELEASE_FIELDS = (
    "tenant_id",
    "survey_id",
    "version_number",
    "contract_version",
    "snapshot_json",
    "snapshot_sha256",
    "policy_sha256",
    "eligibility_policy_version",
    "consent_policy_version",
    "created_by_user_id",
    "create_idempotency_key",
    "create_request_hash",
    "created_at",
    "published_by_user_id",
    "published_at",
    "publish_idempotency_key",
    "publish_request_hash",
)


@event.listens_for(SurveyGovernanceRelease, "before_update")
def _published_survey_release_is_immutable(_mapper, connection, target) -> None:
    row = connection.execute(
        select(SurveyGovernanceRelease.__table__).where(
            SurveyGovernanceRelease.__table__.c.id == target.id
        )
    ).mappings().one_or_none()
    if row is None or row["status"] == "draft":
        return
    if row["status"] == "closed":
        raise ValueError("closed survey governance releases are immutable")
    for field in _IMMUTABLE_RELEASE_FIELDS:
        if getattr(target, field) != row[field]:
            raise ValueError("published survey governance releases are immutable")
    if target.status != "closed":
        raise ValueError("published survey governance releases only allow close")


@event.listens_for(SurveyGovernanceRelease, "before_delete")
def _published_survey_release_cannot_be_deleted(_mapper, connection, target) -> None:
    status = connection.execute(
        select(SurveyGovernanceRelease.__table__.c.status).where(
            SurveyGovernanceRelease.__table__.c.id == target.id
        )
    ).scalar_one_or_none()
    if status in {"published", "closed"}:
        raise ValueError("published survey governance releases cannot be deleted")
