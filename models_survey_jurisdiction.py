"""Server-owned survey jurisdiction bindings and append-only review receipts."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, event

from database import db


SURVEY_CONTENT_RECEIPT_CONTRACT_VERSION = "surveys.content_receipt.v1"
SURVEY_CONTENT_ORIGINS = (
    "manual",
    "template_catalog",
    "draft_materialization",
    "duplicate",
    "seed_demo",
    "import",
    "legacy_unverified",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SurveyContentReceipt(db.Model):
    """An immutable hash-chained receipt for survey content governance."""

    __tablename__ = "survey_content_receipt"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    survey_id = db.Column(db.Integer, nullable=False, index=True)
    contract_version = db.Column(
        db.String(48),
        nullable=False,
        default=SURVEY_CONTENT_RECEIPT_CONTRACT_VERSION,
        server_default=SURVEY_CONTENT_RECEIPT_CONTRACT_VERSION,
    )
    event_type = db.Column(db.String(32), nullable=False, index=True)
    decision = db.Column(db.String(20), nullable=False)
    content_sha256 = db.Column(db.String(64), nullable=False, index=True)
    request_content_sha256 = db.Column(db.String(64), nullable=True)
    jurisdiction_ref = db.Column(db.String(160), nullable=True)
    content_origin = db.Column(
        db.String(32), nullable=False, default="legacy_unverified"
    )
    content_origin_ref = db.Column(db.String(255), nullable=True)
    actor_user_id = db.Column(db.Integer, nullable=True)
    evidence_ref = db.Column(db.String(255), nullable=True)
    reason_code = db.Column(db.String(80), nullable=True)
    idempotency_key = db.Column(db.String(128), nullable=True)
    previous_receipt_sha256 = db.Column(db.String(64), nullable=True)
    receipt_json = db.Column(db.Text, nullable=False)
    receipt_sha256 = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utc_now, index=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "survey_id"],
            ["enc_encuesta.tenant_id", "enc_encuesta.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        CheckConstraint(
            "event_type IN ('created', 'updated', 'rebound', "
            "'review_approved', 'review_blocked', 'published')",
            name="ck_survey_content_receipt_event",
        ),
        CheckConstraint(
            "decision IN ('recorded', 'approved', 'blocked', 'published')",
            name="ck_survey_content_receipt_decision",
        ),
        CheckConstraint(
            "content_origin IN ('manual', 'template_catalog', "
            "'draft_materialization', 'duplicate', 'seed_demo', 'import', "
            "'legacy_unverified')",
            name="ck_survey_content_receipt_origin",
        ),
        CheckConstraint(
            "length(content_sha256) = 64 AND length(receipt_sha256) = 64",
            name="ck_survey_content_receipt_hashes",
        ),
        CheckConstraint(
            "request_content_sha256 IS NULL OR length(request_content_sha256) = 64",
            name="ck_survey_content_receipt_request_hash",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_content_receipt_tenant_idempotency",
        ),
        db.Index(
            "ix_survey_content_receipt_survey_order",
            "tenant_id",
            "survey_id",
            "id",
        ),
    )


@event.listens_for(SurveyContentReceipt, "before_update")
def _survey_content_receipt_cannot_be_updated(*_args) -> None:
    raise ValueError("survey content receipts are immutable")


@event.listens_for(SurveyContentReceipt, "before_delete")
def _survey_content_receipt_cannot_be_deleted(*_args) -> None:
    raise ValueError("survey content receipts are append-only")
