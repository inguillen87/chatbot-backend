"""add durable tenant-scoped survey drafts

Revision ID: 20260728_survey_drafts
Revises: 20260714_wa_flow_interaction
Create Date: 2026-07-28 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260728_survey_drafts"
down_revision = "20260714_wa_flow_interaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "survey_draft",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "schema_version",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'survey-draft.v1'"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("revision >= 1", name="ck_survey_draft_revision_positive"),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "draft_id", name="uq_survey_draft_tenant_draft"),
    )
    op.create_index(op.f("ix_survey_draft_tenant_id"), "survey_draft", ["tenant_id"], unique=False)
    op.create_index(
        "ix_survey_draft_tenant_updated",
        "survey_draft",
        ["tenant_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "survey_draft_idempotency",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_draft_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("draft_id", sa.String(length=160), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "applied_revision >= 1",
            name="ck_survey_draft_idempotency_revision_positive",
        ),
        sa.ForeignKeyConstraint(["survey_draft_id"], ["survey_draft.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_draft_idempotency_tenant_key",
        ),
    )
    op.create_index(
        op.f("ix_survey_draft_idempotency_survey_draft_id"),
        "survey_draft_idempotency",
        ["survey_draft_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_survey_draft_idempotency_tenant_id"),
        "survey_draft_idempotency",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_survey_draft_idempotency_tenant_draft",
        "survey_draft_idempotency",
        ["tenant_id", "survey_draft_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_survey_draft_idempotency_tenant_draft",
        table_name="survey_draft_idempotency",
    )
    op.drop_index(
        op.f("ix_survey_draft_idempotency_tenant_id"),
        table_name="survey_draft_idempotency",
    )
    op.drop_index(
        op.f("ix_survey_draft_idempotency_survey_draft_id"),
        table_name="survey_draft_idempotency",
    )
    op.drop_table("survey_draft_idempotency")
    op.drop_index("ix_survey_draft_tenant_updated", table_name="survey_draft")
    op.drop_index(op.f("ix_survey_draft_tenant_id"), table_name="survey_draft")
    op.drop_table("survey_draft")
