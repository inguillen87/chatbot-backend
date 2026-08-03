"""Add tenant-scoped immutable WhatsApp workflow control-plane ledgers.

Revision ID: 20260802_whatsapp_workflow_v1
Revises: 20260802_survey_eligibility_v1
Create Date: 2026-08-02
"""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260802_whatsapp_workflow_v1"
down_revision = "20260802_survey_eligibility_v1"
branch_labels = None
depends_on = None


_TABLES = (
    "whatsapp_workflow_draft_revision",
    "whatsapp_workflow_review",
    "whatsapp_workflow_version",
    "whatsapp_workflow_activation",
)

_POSTGRES_DOWNGRADE_LOCK_SQL = (
    "LOCK TABLE whatsapp_workflow_activation, whatsapp_workflow_version, "
    "whatsapp_workflow_review, whatsapp_workflow_draft_revision "
    "IN ACCESS EXCLUSIVE MODE"
)


def _json_type():
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _create_tables() -> None:
    op.create_table(
        "whatsapp_workflow_draft_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("draft_digest", sa.String(length=64), nullable=False),
        sa.Column("draft_json", _json_type(), nullable=False),
        sa.Column("authored_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_wa_workflow_draft_revision_positive",
        ),
        sa.CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36",
            name="ck_wa_workflow_draft_ids",
        ),
        sa.CheckConstraint(
            "length(draft_digest) = 64 AND length(request_hash) = 64",
            name="ck_wa_workflow_draft_hashes",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_draft_idempotency_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            name="fk_wa_workflow_draft_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authored_by_user_id"],
            ["user.id"],
            name="fk_wa_workflow_draft_author",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wa_workflow_draft_revision"),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "revision",
            name="uq_wa_workflow_draft_tenant_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_draft_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_draft_tenant_workflow_id",
        ),
    )
    op.create_index(
        "ix_wa_workflow_draft_tenant_workflow_created",
        "whatsapp_workflow_draft_revision",
        ["tenant_id", "workflow_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "whatsapp_workflow_review",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.String(length=36), nullable=False),
        sa.Column("subject_digest", sa.String(length=64), nullable=False),
        sa.Column("subject_sequence", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "operation IN ('publish', 'rollback')",
            name="ck_wa_workflow_review_operation",
        ),
        sa.CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36 AND length(subject_id) = 36",
            name="ck_wa_workflow_review_ids",
        ),
        sa.CheckConstraint(
            "subject_type IN ('draft_revision', 'published_version')",
            name="ck_wa_workflow_review_subject_type",
        ),
        sa.CheckConstraint(
            "(operation = 'publish' AND subject_type = 'draft_revision') OR "
            "(operation = 'rollback' AND subject_type = 'published_version')",
            name="ck_wa_workflow_review_subject_operation",
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_wa_workflow_review_decision",
        ),
        sa.CheckConstraint(
            "subject_sequence > 0",
            name="ck_wa_workflow_review_subject_sequence_positive",
        ),
        sa.CheckConstraint(
            "length(subject_digest) = 64 AND length(request_hash) = 64",
            name="ck_wa_workflow_review_hashes",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_review_idempotency_key",
        ),
        sa.CheckConstraint(
            "length(review_note) > 0 AND length(review_note) <= 1000",
            name="ck_wa_workflow_review_note",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            name="fk_wa_workflow_review_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["user.id"],
            name="fk_wa_workflow_review_reviewer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wa_workflow_review"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_review_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_review_tenant_workflow_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "operation",
            "subject_id",
            "subject_digest",
            "subject_sequence",
            name="uq_wa_workflow_review_subject_sequence",
        ),
    )
    op.create_index(
        "ix_wa_workflow_review_tenant_subject",
        "whatsapp_workflow_review",
        [
            "tenant_id",
            "workflow_id",
            "operation",
            "subject_id",
            "subject_digest",
            "subject_sequence",
        ],
        unique=False,
    )

    op.create_table(
        "whatsapp_workflow_version",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("version_kind", sa.String(length=16), nullable=False),
        sa.Column("source_draft_revision_id", sa.String(length=36), nullable=False),
        sa.Column("restored_from_version_id", sa.String(length=36), nullable=True),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("content_json", _json_type(), nullable=False),
        sa.Column("published_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_wa_workflow_version_positive",
        ),
        sa.CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36 "
            "AND length(source_draft_revision_id) = 36 AND length(review_id) = 36 "
            "AND (restored_from_version_id IS NULL OR length(restored_from_version_id) = 36)",
            name="ck_wa_workflow_version_ids",
        ),
        sa.CheckConstraint(
            "version_kind IN ('publish', 'rollback')",
            name="ck_wa_workflow_version_kind",
        ),
        sa.CheckConstraint(
            "(version_kind = 'publish' AND restored_from_version_id IS NULL) OR "
            "(version_kind = 'rollback' AND restored_from_version_id IS NOT NULL)",
            name="ck_wa_workflow_version_restore_source",
        ),
        sa.CheckConstraint(
            "length(content_digest) = 64 AND length(request_hash) = 64",
            name="ck_wa_workflow_version_hashes",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_version_idempotency_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            name="fk_wa_workflow_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"],
            ["user.id"],
            name="fk_wa_workflow_version_publisher",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "source_draft_revision_id"],
            [
                "whatsapp_workflow_draft_revision.tenant_id",
                "whatsapp_workflow_draft_revision.workflow_id",
                "whatsapp_workflow_draft_revision.id",
            ],
            name="fk_wa_workflow_version_source_draft",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "review_id"],
            [
                "whatsapp_workflow_review.tenant_id",
                "whatsapp_workflow_review.workflow_id",
                "whatsapp_workflow_review.id",
            ],
            name="fk_wa_workflow_version_review",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "restored_from_version_id"],
            [
                "whatsapp_workflow_version.tenant_id",
                "whatsapp_workflow_version.workflow_id",
                "whatsapp_workflow_version.id",
            ],
            name="fk_wa_workflow_version_restore_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wa_workflow_version"),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "version",
            name="uq_wa_workflow_version_tenant_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_version_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "review_id",
            name="uq_wa_workflow_version_tenant_review",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_version_tenant_workflow_id",
        ),
    )
    op.create_index(
        "ix_wa_workflow_version_tenant_workflow_published",
        "whatsapp_workflow_version",
        ["tenant_id", "workflow_id", "published_at"],
        unique=False,
    )

    op.create_table(
        "whatsapp_workflow_activation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("previous_activation_id", sa.String(length=36), nullable=True),
        sa.Column("activation_kind", sa.String(length=16), nullable=False),
        sa.Column("activated_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_wa_workflow_activation_positive",
        ),
        sa.CheckConstraint(
            "length(id) = 36 AND length(workflow_id) = 36 "
            "AND length(workflow_version_id) = 36 "
            "AND (previous_activation_id IS NULL OR length(previous_activation_id) = 36)",
            name="ck_wa_workflow_activation_ids",
        ),
        sa.CheckConstraint(
            "activation_kind IN ('publish', 'rollback')",
            name="ck_wa_workflow_activation_kind",
        ),
        sa.CheckConstraint(
            "(sequence = 1 AND previous_activation_id IS NULL) OR "
            "(sequence > 1 AND previous_activation_id IS NOT NULL)",
            name="ck_wa_workflow_activation_predecessor",
        ),
        sa.CheckConstraint(
            "previous_activation_id IS NULL OR previous_activation_id <> id",
            name="ck_wa_workflow_activation_not_self",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_wa_workflow_activation_request_hash",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) >= 8 AND length(idempotency_key) <= 128",
            name="ck_wa_workflow_activation_idempotency_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            name="fk_wa_workflow_activation_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_user_id"],
            ["user.id"],
            name="fk_wa_workflow_activation_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "workflow_version_id"],
            [
                "whatsapp_workflow_version.tenant_id",
                "whatsapp_workflow_version.workflow_id",
                "whatsapp_workflow_version.id",
            ],
            name="fk_wa_workflow_activation_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id", "previous_activation_id"],
            [
                "whatsapp_workflow_activation.tenant_id",
                "whatsapp_workflow_activation.workflow_id",
                "whatsapp_workflow_activation.id",
            ],
            name="fk_wa_workflow_activation_previous",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wa_workflow_activation"),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "sequence",
            name="uq_wa_workflow_activation_tenant_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_version_id",
            name="uq_wa_workflow_activation_tenant_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_wa_workflow_activation_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "id",
            name="uq_wa_workflow_activation_tenant_workflow_id",
        ),
    )
    op.create_index(
        "ix_wa_workflow_activation_tenant_workflow_activated",
        "whatsapp_workflow_activation",
        ["tenant_id", "workflow_id", "activated_at"],
        unique=False,
    )


def _create_sqlite_guards() -> None:
    for table in _TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_update_immutable
            BEFORE UPDATE ON {table}
            FOR EACH ROW BEGIN
                SELECT RAISE(ABORT, 'WhatsApp workflow history is immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_delete_immutable
            BEFORE DELETE ON {table}
            FOR EACH ROW BEGIN
                SELECT RAISE(ABORT, 'WhatsApp workflow history is immutable');
            END
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_draft_contiguous
        BEFORE INSERT ON whatsapp_workflow_draft_revision
        FOR EACH ROW BEGIN
            SELECT CASE
                WHEN NEW.revision = 1 AND EXISTS (
                    SELECT 1 FROM whatsapp_workflow_draft_revision
                    WHERE tenant_id = NEW.tenant_id AND workflow_id = NEW.workflow_id
                ) THEN RAISE(ABORT, 'workflow first revision already exists')
                WHEN NEW.revision > 1 AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_draft_revision
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND revision = NEW.revision - 1
                ) THEN RAISE(ABORT, 'workflow draft revision must be contiguous')
            END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_review_subject
        BEFORE INSERT ON whatsapp_workflow_review
        FOR EACH ROW BEGIN
            SELECT CASE
                WHEN NEW.operation = 'publish' AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_draft_revision
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND id = NEW.subject_id
                      AND draft_digest = NEW.subject_digest
                ) THEN RAISE(ABORT, 'workflow publish review subject mismatch')
                WHEN NEW.operation = 'rollback' AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_version
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND id = NEW.subject_id
                      AND content_digest = NEW.subject_digest
                ) THEN RAISE(ABORT, 'workflow rollback review subject mismatch')
                WHEN NEW.subject_sequence = 1 AND EXISTS (
                    SELECT 1 FROM whatsapp_workflow_review
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND operation = NEW.operation
                      AND subject_id = NEW.subject_id
                      AND subject_digest = NEW.subject_digest
                ) THEN RAISE(ABORT, 'workflow first review sequence already exists')
                WHEN NEW.subject_sequence > 1 AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_review
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND operation = NEW.operation
                      AND subject_id = NEW.subject_id
                      AND subject_digest = NEW.subject_digest
                      AND subject_sequence = NEW.subject_sequence - 1
                ) THEN RAISE(ABORT, 'workflow review sequence must be contiguous')
            END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_version_review
        BEFORE INSERT ON whatsapp_workflow_version
        FOR EACH ROW BEGIN
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_review AS approval
                    WHERE approval.tenant_id = NEW.tenant_id
                      AND approval.workflow_id = NEW.workflow_id
                      AND approval.id = NEW.review_id
                      AND approval.decision = 'approved'
                      AND approval.reviewed_by_user_id <> NEW.published_by_user_id
                      AND (
                          NEW.version_kind <> 'publish'
                          OR approval.reviewed_by_user_id <> (
                              SELECT authored_by_user_id
                              FROM whatsapp_workflow_draft_revision
                              WHERE tenant_id = NEW.tenant_id
                                AND workflow_id = NEW.workflow_id
                                AND id = NEW.source_draft_revision_id
                          )
                      )
                      AND approval.subject_digest = NEW.content_digest
                      AND (
                          (NEW.version_kind = 'publish'
                           AND approval.operation = 'publish'
                           AND approval.subject_id = NEW.source_draft_revision_id)
                          OR
                          (NEW.version_kind = 'rollback'
                           AND approval.operation = 'rollback'
                           AND approval.subject_id = NEW.restored_from_version_id)
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM whatsapp_workflow_review AS later
                          WHERE later.tenant_id = approval.tenant_id
                            AND later.workflow_id = approval.workflow_id
                            AND later.operation = approval.operation
                            AND later.subject_id = approval.subject_id
                            AND later.subject_digest = approval.subject_digest
                            AND later.subject_sequence > approval.subject_sequence
                      )
                ) THEN RAISE(ABORT, 'workflow version requires exact independent approval')
                WHEN NEW.version = 1 AND EXISTS (
                    SELECT 1 FROM whatsapp_workflow_version
                    WHERE tenant_id = NEW.tenant_id AND workflow_id = NEW.workflow_id
                ) THEN RAISE(ABORT, 'workflow first version already exists')
                WHEN NEW.version > 1 AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_version
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND version = NEW.version - 1
                ) THEN RAISE(ABORT, 'workflow version must be contiguous')
                WHEN NEW.version > 1 AND EXISTS (
                    SELECT 1 FROM whatsapp_workflow_version
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND version = NEW.version - 1
                      AND content_digest = NEW.content_digest
                ) THEN RAISE(ABORT, 'workflow version content already active')
                WHEN NEW.version_kind = 'publish' AND EXISTS (
                    SELECT 1
                    FROM whatsapp_workflow_draft_revision AS source
                    JOIN whatsapp_workflow_draft_revision AS later
                      ON later.tenant_id = source.tenant_id
                     AND later.workflow_id = source.workflow_id
                     AND later.revision > source.revision
                    WHERE source.tenant_id = NEW.tenant_id
                      AND source.workflow_id = NEW.workflow_id
                      AND source.id = NEW.source_draft_revision_id
                ) THEN RAISE(ABORT, 'workflow published draft is stale')
                WHEN NEW.version_kind = 'publish' AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_draft_revision
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND id = NEW.source_draft_revision_id
                      AND draft_digest = NEW.content_digest
                      AND json(draft_json) = json(NEW.content_json)
                ) THEN RAISE(ABORT, 'workflow published content mismatch')
                WHEN NEW.version_kind = 'rollback' AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_version
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND id = NEW.restored_from_version_id
                      AND content_digest = NEW.content_digest
                      AND json(content_json) = json(NEW.content_json)
                ) THEN RAISE(ABORT, 'workflow rollback content mismatch')
            END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_activation_ledger
        BEFORE INSERT ON whatsapp_workflow_activation
        FOR EACH ROW BEGIN
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_version
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND id = NEW.workflow_version_id
                      AND version = NEW.sequence
                      AND version_kind = NEW.activation_kind
                      AND idempotency_key = NEW.idempotency_key
                      AND request_hash = NEW.request_hash
                ) THEN RAISE(ABORT, 'workflow activation/version mismatch')
                WHEN NEW.sequence > 1 AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_workflow_activation
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_id = NEW.workflow_id
                      AND id = NEW.previous_activation_id
                      AND sequence = NEW.sequence - 1
                ) THEN RAISE(ABORT, 'workflow activation must extend previous sequence')
            END;
        END
        """
    )


def _create_postgresql_guards() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wa_workflow_history_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'WhatsApp workflow history is immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in _TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION wa_workflow_history_immutable()
            """
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wa_workflow_draft_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.revision = 1 AND EXISTS (
                SELECT 1 FROM whatsapp_workflow_draft_revision
                WHERE tenant_id = NEW.tenant_id AND workflow_id = NEW.workflow_id
            ) THEN
                RAISE EXCEPTION 'workflow first revision already exists';
            END IF;
            IF NEW.revision > 1 AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_draft_revision
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND revision = NEW.revision - 1
            ) THEN
                RAISE EXCEPTION 'workflow draft revision must be contiguous';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_draft_contiguous
        BEFORE INSERT ON whatsapp_workflow_draft_revision
        FOR EACH ROW EXECUTE FUNCTION wa_workflow_draft_guard()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wa_workflow_review_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.operation = 'publish' AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_draft_revision
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND id = NEW.subject_id
                  AND draft_digest = NEW.subject_digest
            ) THEN
                RAISE EXCEPTION 'workflow publish review subject mismatch';
            END IF;
            IF NEW.operation = 'rollback' AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_version
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND id = NEW.subject_id
                  AND content_digest = NEW.subject_digest
            ) THEN
                RAISE EXCEPTION 'workflow rollback review subject mismatch';
            END IF;
            IF NEW.subject_sequence = 1 AND EXISTS (
                SELECT 1 FROM whatsapp_workflow_review
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND operation = NEW.operation
                  AND subject_id = NEW.subject_id
                  AND subject_digest = NEW.subject_digest
            ) THEN
                RAISE EXCEPTION 'workflow first review sequence already exists';
            END IF;
            IF NEW.subject_sequence > 1 AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_review
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND operation = NEW.operation
                  AND subject_id = NEW.subject_id
                  AND subject_digest = NEW.subject_digest
                  AND subject_sequence = NEW.subject_sequence - 1
            ) THEN
                RAISE EXCEPTION 'workflow review sequence must be contiguous';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_review_subject
        BEFORE INSERT ON whatsapp_workflow_review
        FOR EACH ROW EXECUTE FUNCTION wa_workflow_review_guard()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wa_workflow_version_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_review AS approval
                WHERE approval.tenant_id = NEW.tenant_id
                  AND approval.workflow_id = NEW.workflow_id
                  AND approval.id = NEW.review_id
                  AND approval.decision = 'approved'
                  AND approval.reviewed_by_user_id <> NEW.published_by_user_id
                  AND (
                      NEW.version_kind <> 'publish'
                      OR approval.reviewed_by_user_id <> (
                          SELECT authored_by_user_id
                          FROM whatsapp_workflow_draft_revision
                          WHERE tenant_id = NEW.tenant_id
                            AND workflow_id = NEW.workflow_id
                            AND id = NEW.source_draft_revision_id
                      )
                  )
                  AND approval.subject_digest = NEW.content_digest
                  AND (
                      (NEW.version_kind = 'publish'
                       AND approval.operation = 'publish'
                       AND approval.subject_id = NEW.source_draft_revision_id)
                      OR
                      (NEW.version_kind = 'rollback'
                       AND approval.operation = 'rollback'
                       AND approval.subject_id = NEW.restored_from_version_id)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM whatsapp_workflow_review AS later
                      WHERE later.tenant_id = approval.tenant_id
                        AND later.workflow_id = approval.workflow_id
                        AND later.operation = approval.operation
                        AND later.subject_id = approval.subject_id
                        AND later.subject_digest = approval.subject_digest
                        AND later.subject_sequence > approval.subject_sequence
                  )
            ) THEN
                RAISE EXCEPTION 'workflow version requires exact independent approval';
            END IF;
            IF NEW.version = 1 AND EXISTS (
                SELECT 1 FROM whatsapp_workflow_version
                WHERE tenant_id = NEW.tenant_id AND workflow_id = NEW.workflow_id
            ) THEN
                RAISE EXCEPTION 'workflow first version already exists';
            END IF;
            IF NEW.version > 1 AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_version
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND version = NEW.version - 1
            ) THEN
                RAISE EXCEPTION 'workflow version must be contiguous';
            END IF;
            IF NEW.version > 1 AND EXISTS (
                SELECT 1 FROM whatsapp_workflow_version
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND version = NEW.version - 1
                  AND content_digest = NEW.content_digest
            ) THEN
                RAISE EXCEPTION 'workflow version content already active';
            END IF;
            IF NEW.version_kind = 'publish' AND EXISTS (
                SELECT 1
                FROM whatsapp_workflow_draft_revision AS source
                JOIN whatsapp_workflow_draft_revision AS later
                  ON later.tenant_id = source.tenant_id
                 AND later.workflow_id = source.workflow_id
                 AND later.revision > source.revision
                WHERE source.tenant_id = NEW.tenant_id
                  AND source.workflow_id = NEW.workflow_id
                  AND source.id = NEW.source_draft_revision_id
            ) THEN
                RAISE EXCEPTION 'workflow published draft is stale';
            END IF;
            IF NEW.version_kind = 'publish' AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_draft_revision
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND id = NEW.source_draft_revision_id
                  AND draft_digest = NEW.content_digest
                  AND draft_json = NEW.content_json
            ) THEN
                RAISE EXCEPTION 'workflow published content mismatch';
            END IF;
            IF NEW.version_kind = 'rollback' AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_version
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND id = NEW.restored_from_version_id
                  AND content_digest = NEW.content_digest
                  AND content_json = NEW.content_json
            ) THEN
                RAISE EXCEPTION 'workflow rollback content mismatch';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_version_review
        BEFORE INSERT ON whatsapp_workflow_version
        FOR EACH ROW EXECUTE FUNCTION wa_workflow_version_guard()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wa_workflow_activation_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_version
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND id = NEW.workflow_version_id
                  AND version = NEW.sequence
                  AND version_kind = NEW.activation_kind
                  AND idempotency_key = NEW.idempotency_key
                  AND request_hash = NEW.request_hash
            ) THEN
                RAISE EXCEPTION 'workflow activation/version mismatch';
            END IF;
            IF NEW.sequence > 1 AND NOT EXISTS (
                SELECT 1 FROM whatsapp_workflow_activation
                WHERE tenant_id = NEW.tenant_id
                  AND workflow_id = NEW.workflow_id
                  AND id = NEW.previous_activation_id
                  AND sequence = NEW.sequence - 1
            ) THEN
                RAISE EXCEPTION 'workflow activation must extend previous sequence';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_wa_workflow_activation_ledger
        BEFORE INSERT ON whatsapp_workflow_activation
        FOR EACH ROW EXECUTE FUNCTION wa_workflow_activation_guard()
        """
    )


def _drop_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_wa_workflow_activation_ledger "
            "ON whatsapp_workflow_activation"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_wa_workflow_version_review "
            "ON whatsapp_workflow_version"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_wa_workflow_review_subject "
            "ON whatsapp_workflow_review"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_wa_workflow_draft_contiguous "
            "ON whatsapp_workflow_draft_revision"
        )
        for table in reversed(_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute("DROP FUNCTION IF EXISTS wa_workflow_activation_guard()")
        op.execute("DROP FUNCTION IF EXISTS wa_workflow_version_guard()")
        op.execute("DROP FUNCTION IF EXISTS wa_workflow_review_guard()")
        op.execute("DROP FUNCTION IF EXISTS wa_workflow_draft_guard()")
        op.execute("DROP FUNCTION IF EXISTS wa_workflow_history_immutable()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_wa_workflow_activation_ledger")
        op.execute("DROP TRIGGER IF EXISTS trg_wa_workflow_version_review")
        op.execute("DROP TRIGGER IF EXISTS trg_wa_workflow_review_subject")
        op.execute("DROP TRIGGER IF EXISTS trg_wa_workflow_draft_contiguous")
        for table in reversed(_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_update_immutable")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_delete_immutable")


def upgrade() -> None:
    _create_tables()
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _create_postgresql_guards()
    elif dialect == "sqlite":
        _create_sqlite_guards()


def _is_offline_mode() -> bool:
    try:
        return bool(context.is_offline_mode())
    except (AttributeError, NameError):
        # Direct migration unit tests install an Operations proxy without a
        # full Alembic EnvironmentContext. They are online database tests.
        return False


def _lock_history_for_downgrade(bind) -> None:
    dialect = bind.dialect.name
    if dialect == "postgresql":
        # One transaction-scoped lock closes the COUNT -> DROP insertion race.
        bind.execute(sa.text(_POSTGRES_DOWNGRADE_LOCK_SQL))
    elif dialect == "sqlite":
        # SQLite has no LOCK TABLE. A zero-row DML statement upgrades the
        # current transaction to a writer before COUNT without touching the
        # immutable ledger. Concurrent writers then wait/fail before the check.
        bind.execute(
            sa.text(
                "UPDATE whatsapp_workflow_draft_revision SET id = id WHERE 0"
            )
        )
        in_transaction = getattr(bind, "in_transaction", None)
        if not callable(in_transaction) or not in_transaction():
            raise RuntimeError(
                "SQLite workflow downgrade requires an explicit write transaction"
            )


def _workflow_history_row_count(bind) -> int:
    return bind.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM whatsapp_workflow_draft_revision) + "
            "(SELECT COUNT(*) FROM whatsapp_workflow_review) + "
            "(SELECT COUNT(*) FROM whatsapp_workflow_version) + "
            "(SELECT COUNT(*) FROM whatsapp_workflow_activation)"
        )
    ).scalar_one()


def downgrade() -> None:
    if _is_offline_mode():
        raise RuntimeError(
            "Offline downgrade refused: workflow history cannot be checked for data loss"
        )
    bind = op.get_bind()
    _lock_history_for_downgrade(bind)
    total = _workflow_history_row_count(bind)
    if total:
        raise RuntimeError(
            "WhatsApp workflow history exists; downgrade would erase audit records"
        )

    _drop_guards()
    op.drop_index(
        "ix_wa_workflow_activation_tenant_workflow_activated",
        table_name="whatsapp_workflow_activation",
    )
    op.drop_table("whatsapp_workflow_activation")
    op.drop_index(
        "ix_wa_workflow_version_tenant_workflow_published",
        table_name="whatsapp_workflow_version",
    )
    op.drop_table("whatsapp_workflow_version")
    op.drop_index(
        "ix_wa_workflow_review_tenant_subject",
        table_name="whatsapp_workflow_review",
    )
    op.drop_table("whatsapp_workflow_review")
    op.drop_index(
        "ix_wa_workflow_draft_tenant_workflow_created",
        table_name="whatsapp_workflow_draft_revision",
    )
    op.drop_table("whatsapp_workflow_draft_revision")
