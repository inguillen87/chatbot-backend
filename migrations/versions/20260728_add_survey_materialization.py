"""materialize durable survey drafts without losing canonical references

Revision ID: 20260728_survey_materialize
Revises: 20260728_survey_guard
Create Date: 2026-07-28 00:00:04.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_survey_materialize"
down_revision = "20260728_survey_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "enc_encuesta",
        sa.Column("document_ref", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "enc_pregunta",
        sa.Column("logical_ref", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "enc_opcion",
        sa.Column("logical_ref", sa.String(length=160), nullable=True),
    )
    op.create_index(
        "uq_enc_pregunta_encuesta_logical_ref",
        "enc_pregunta",
        ["encuesta_id", "logical_ref"],
        unique=True,
    )
    op.create_index(
        "uq_enc_opcion_pregunta_logical_ref",
        "enc_opcion",
        ["pregunta_id", "logical_ref"],
        unique=True,
    )

    op.create_table(
        "survey_draft_materialization",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_draft_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.String(length=160), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("document_ref", sa.String(length=160), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("operation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "draft_revision >= 1",
            name="ck_survey_materialization_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["user.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["survey_draft_id"],
            ["survey_draft.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["survey_id"],
            ["enc_encuesta.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "survey_id",
            name="uq_survey_materialization_survey_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "survey_draft_id",
            "draft_revision",
            name="uq_survey_materialization_tenant_draft_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_materialization_tenant_key",
        ),
    )
    op.create_index(
        op.f("ix_survey_draft_materialization_tenant_id"),
        "survey_draft_materialization",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_survey_draft_materialization_survey_draft_id"),
        "survey_draft_materialization",
        ["survey_draft_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_survey_draft_materialization_survey_id"),
        "survey_draft_materialization",
        ["survey_id"],
        unique=False,
    )
    op.create_index(
        "ix_survey_materialization_tenant_draft_id_revision",
        "survey_draft_materialization",
        ["tenant_id", "draft_id", "draft_revision"],
        unique=False,
    )

    op.create_table(
        "survey_draft_materialization_alias",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("materialization_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["materialization_id"],
            ["survey_draft_materialization.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_materialization_alias_tenant_key",
        ),
    )
    op.create_index(
        op.f("ix_survey_draft_materialization_alias_tenant_id"),
        "survey_draft_materialization_alias",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_survey_draft_materialization_alias_materialization_id"),
        "survey_draft_materialization_alias",
        ["materialization_id"],
        unique=False,
    )
    op.create_index(
        "ix_survey_materialization_alias_tenant_receipt",
        "survey_draft_materialization_alias",
        ["tenant_id", "materialization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_survey_materialization_alias_tenant_receipt",
        table_name="survey_draft_materialization_alias",
    )
    op.drop_index(
        op.f("ix_survey_draft_materialization_alias_materialization_id"),
        table_name="survey_draft_materialization_alias",
    )
    op.drop_index(
        op.f("ix_survey_draft_materialization_alias_tenant_id"),
        table_name="survey_draft_materialization_alias",
    )
    op.drop_table("survey_draft_materialization_alias")

    op.drop_index(
        "ix_survey_materialization_tenant_draft_id_revision",
        table_name="survey_draft_materialization",
    )
    op.drop_index(
        op.f("ix_survey_draft_materialization_survey_id"),
        table_name="survey_draft_materialization",
    )
    op.drop_index(
        op.f("ix_survey_draft_materialization_survey_draft_id"),
        table_name="survey_draft_materialization",
    )
    op.drop_index(
        op.f("ix_survey_draft_materialization_tenant_id"),
        table_name="survey_draft_materialization",
    )
    op.drop_table("survey_draft_materialization")

    op.drop_index(
        "uq_enc_opcion_pregunta_logical_ref",
        table_name="enc_opcion",
    )
    op.drop_index(
        "uq_enc_pregunta_encuesta_logical_ref",
        table_name="enc_pregunta",
    )
    op.drop_column("enc_opcion", "logical_ref")
    op.drop_column("enc_pregunta", "logical_ref")
    op.drop_column("enc_encuesta", "document_ref")
