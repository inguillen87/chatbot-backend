from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, event

from models import JSONType, db


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class WhatsAppWorkflowDraftRevision(db.Model):
    """Append-only durable editing history for one tenant workflow."""

    __tablename__ = "whatsapp_workflow_draft_revision"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_id = db.Column(db.String(36), nullable=False)
    revision = db.Column(db.Integer, nullable=False)
    schema_version = db.Column(db.String(64), nullable=False)
    draft_digest = db.Column(db.String(64), nullable=False)
    draft_json = db.Column(JSONType, nullable=False)
    authored_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=db.func.now(),
    )

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_wa_workflow_draft_revision_positive"),
        CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36",
            name="ck_wa_workflow_draft_ids",
        ),
        CheckConstraint(
            "length(draft_digest) = 64 AND length(request_hash) = 64",
            name="ck_wa_workflow_draft_hashes",
        ),
        CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_draft_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "revision",
            name="uq_wa_workflow_draft_tenant_revision",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_draft_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_draft_tenant_workflow_id",
        ),
        db.Index(
            "ix_wa_workflow_draft_tenant_workflow_created",
            "tenant_id",
            "workflow_id",
            "created_at",
        ),
    )


class WhatsAppWorkflowReview(db.Model):
    """Immutable human review bound to one exact draft/version digest."""

    __tablename__ = "whatsapp_workflow_review"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_id = db.Column(db.String(36), nullable=False)
    operation = db.Column(db.String(16), nullable=False)
    subject_type = db.Column(db.String(24), nullable=False)
    subject_id = db.Column(db.String(36), nullable=False)
    subject_digest = db.Column(db.String(64), nullable=False)
    subject_sequence = db.Column(db.Integer, nullable=False)
    decision = db.Column(db.String(16), nullable=False)
    review_note = db.Column(db.Text, nullable=False)
    reviewed_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    reviewed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=db.func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "operation IN ('publish', 'rollback')",
            name="ck_wa_workflow_review_operation",
        ),
        CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36 AND length(subject_id) = 36",
            name="ck_wa_workflow_review_ids",
        ),
        CheckConstraint(
            "subject_type IN ('draft_revision', 'published_version')",
            name="ck_wa_workflow_review_subject_type",
        ),
        CheckConstraint(
            "(operation = 'publish' AND subject_type = 'draft_revision') OR "
            "(operation = 'rollback' AND subject_type = 'published_version')",
            name="ck_wa_workflow_review_subject_operation",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_wa_workflow_review_decision",
        ),
        CheckConstraint(
            "subject_sequence > 0",
            name="ck_wa_workflow_review_subject_sequence_positive",
        ),
        CheckConstraint(
            "length(subject_digest) = 64 AND length(request_hash) = 64",
            name="ck_wa_workflow_review_hashes",
        ),
        CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_review_idempotency_key",
        ),
        CheckConstraint(
            "length(review_note) > 0 AND length(review_note) <= 1000",
            name="ck_wa_workflow_review_note",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_review_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_review_tenant_workflow_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "operation",
            "subject_id",
            "subject_digest",
            "subject_sequence",
            name="uq_wa_workflow_review_subject_sequence",
        ),
        db.Index(
            "ix_wa_workflow_review_tenant_subject",
            "tenant_id",
            "workflow_id",
            "operation",
            "subject_id",
            "subject_digest",
            "subject_sequence",
        ),
    )


class WhatsAppWorkflowVersion(db.Model):
    """Immutable publication ledger; rollback appends another version."""

    __tablename__ = "whatsapp_workflow_version"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_id = db.Column(db.String(36), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    version_kind = db.Column(db.String(16), nullable=False)
    source_draft_revision_id = db.Column(db.String(36), nullable=False)
    restored_from_version_id = db.Column(db.String(36), nullable=True)
    review_id = db.Column(db.String(36), nullable=False)
    schema_version = db.Column(db.String(64), nullable=False)
    content_digest = db.Column(db.String(64), nullable=False)
    content_json = db.Column(JSONType, nullable=False)
    published_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    published_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=db.func.now(),
    )

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_wa_workflow_version_positive"),
        CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36 "
            "AND length(source_draft_revision_id) = 36 AND length(review_id) = 36 "
            "AND (restored_from_version_id IS NULL OR length(restored_from_version_id) = 36)",
            name="ck_wa_workflow_version_ids",
        ),
        CheckConstraint(
            "version_kind IN ('publish', 'rollback')",
            name="ck_wa_workflow_version_kind",
        ),
        CheckConstraint(
            "(version_kind = 'publish' AND restored_from_version_id IS NULL) OR "
            "(version_kind = 'rollback' AND restored_from_version_id IS NOT NULL)",
            name="ck_wa_workflow_version_restore_source",
        ),
        CheckConstraint(
            "length(content_digest) = 64 AND length(request_hash) = 64",
            name="ck_wa_workflow_version_hashes",
        ),
        CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_version_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "version",
            name="uq_wa_workflow_version_tenant_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_version_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "review_id",
            name="uq_wa_workflow_version_tenant_review",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_version_tenant_workflow_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "source_draft_revision_id"],
            [
                "whatsapp_workflow_draft_revision.tenant_id",
                "whatsapp_workflow_draft_revision.workflow_id",
                "whatsapp_workflow_draft_revision.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "review_id"],
            [
                "whatsapp_workflow_review.tenant_id",
                "whatsapp_workflow_review.workflow_id",
                "whatsapp_workflow_review.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "restored_from_version_id"],
            [
                "whatsapp_workflow_version.tenant_id",
                "whatsapp_workflow_version.workflow_id",
                "whatsapp_workflow_version.id",
            ],
            ondelete="RESTRICT",
        ),
        db.Index(
            "ix_wa_workflow_version_tenant_workflow_published",
            "tenant_id",
            "workflow_id",
            "published_at",
        ),
    )


class WhatsAppWorkflowActivation(db.Model):
    """Append-only activation pointer; latest sequence is the control-plane active version."""

    __tablename__ = "whatsapp_workflow_activation"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    workflow_version_id = db.Column(db.String(36), nullable=False)
    previous_activation_id = db.Column(db.String(36), nullable=True)
    activation_kind = db.Column(db.String(16), nullable=False)
    activated_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    activated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=db.func.now(),
    )

    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_wa_workflow_activation_positive"),
        CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36 "
            "AND length(workflow_version_id) = 36 "
            "AND (previous_activation_id IS NULL OR length(previous_activation_id) = 36)",
            name="ck_wa_workflow_activation_ids",
        ),
        CheckConstraint(
            "activation_kind IN ('publish', 'rollback')",
            name="ck_wa_workflow_activation_kind",
        ),
        CheckConstraint(
            "(sequence = 1 AND previous_activation_id IS NULL) OR "
            "(sequence > 1 AND previous_activation_id IS NOT NULL)",
            name="ck_wa_workflow_activation_predecessor",
        ),
        CheckConstraint(
            "previous_activation_id IS NULL OR previous_activation_id <> id",
            name="ck_wa_workflow_activation_not_self",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_wa_workflow_activation_request_hash",
        ),
        CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_activation_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "sequence",
            name="uq_wa_workflow_activation_tenant_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_version_id",
            name="uq_wa_workflow_activation_tenant_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_activation_tenant_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_activation_tenant_workflow_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "workflow_version_id"],
            [
                "whatsapp_workflow_version.tenant_id",
                "whatsapp_workflow_version.workflow_id",
                "whatsapp_workflow_version.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "previous_activation_id"],
            [
                "whatsapp_workflow_activation.tenant_id",
                "whatsapp_workflow_activation.workflow_id",
                "whatsapp_workflow_activation.id",
            ],
            ondelete="RESTRICT",
        ),
        db.Index(
            "ix_wa_workflow_activation_tenant_workflow_activated",
            "tenant_id",
            "workflow_id",
            "activated_at",
        ),
    )


def _immutable_history(_mapper, _connection, _target) -> None:
    raise ValueError("WhatsApp workflow history is immutable; append a new record")


for _model in (
    WhatsAppWorkflowDraftRevision,
    WhatsAppWorkflowReview,
    WhatsAppWorkflowVersion,
    WhatsAppWorkflowActivation,
):
    event.listen(_model, "before_update", _immutable_history)
    event.listen(_model, "before_delete", _immutable_history)
