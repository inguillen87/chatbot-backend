"""add durable exactly-once receipts for public survey responses

Revision ID: 20260728_survey_response_receipt
Revises: 20260728_survey_materialize
Create Date: 2026-07-28 00:00:05.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_survey_response_receipt"
down_revision = "20260728_survey_materialize"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "survey_response_receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("response_id", sa.Integer(), nullable=False),
        sa.Column("submission_id_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "canonical_version",
            sa.String(length=32),
            nullable=False,
            server_default="survey-response.v1",
        ),
        sa.Column("instrument_revision", sa.Integer(), nullable=False),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="surveys.response_receipt.v1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "instrument_revision >= 1",
            name="ck_survey_response_receipt_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["enc_respuesta.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["survey_id"],
            ["enc_encuesta.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "response_id",
            name="uq_survey_response_receipt_response",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "submission_id_hash",
            name="uq_survey_response_receipt_tenant_submission",
        ),
    )
    op.create_index(
        op.f("ix_survey_response_receipt_tenant_id"),
        "survey_response_receipt",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_survey_response_receipt_survey_id"),
        "survey_response_receipt",
        ["survey_id"],
        unique=False,
    )
    op.create_index(
        "ix_survey_response_receipt_tenant_survey_created",
        "survey_response_receipt",
        ["tenant_id", "survey_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_survey_response_receipt_tenant_survey_created",
        table_name="survey_response_receipt",
    )
    op.drop_index(
        op.f("ix_survey_response_receipt_survey_id"),
        table_name="survey_response_receipt",
    )
    op.drop_index(
        op.f("ix_survey_response_receipt_tenant_id"),
        table_name="survey_response_receipt",
    )
    op.drop_table("survey_response_receipt")
