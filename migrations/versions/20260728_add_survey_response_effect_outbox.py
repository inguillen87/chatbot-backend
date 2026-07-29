"""add durable survey response effect outbox

Revision ID: 20260728_survey_effect_outbox
Revises: 20260728_survey_response_receipt
Create Date: 2026-07-28 00:00:06.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_survey_effect_outbox"
down_revision = "20260728_survey_response_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "points_transaction",
        sa.Column("idempotency_key", sa.String(length=191), nullable=True),
    )
    op.create_index(
        "uq_points_transaction_tenant_idempotency",
        "points_transaction",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )

    op.create_table(
        "survey_response_effect_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("response_id", sa.Integer(), nullable=False),
        sa.Column("effect_type", sa.String(length=32), nullable=False),
        sa.Column("effect_key", sa.String(length=191), nullable=False),
        sa.Column("scope_key", sa.String(length=191), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="8",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="surveys.response_effect.v1",
        ),
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
        sa.CheckConstraint(
            "effect_type IN ('analytics.v1', 'reward.v1', 'realtime.v2')",
            name="ck_survey_response_effect_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_wait', 'succeeded', 'skipped', 'dead')",
            name="ck_survey_response_effect_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_survey_response_effect_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_survey_response_effect_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts",
            name="ck_survey_response_effect_attempts_within_max",
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
            "effect_key",
            name="uq_survey_response_effect_key",
        ),
        sa.UniqueConstraint(
            "response_id",
            "effect_type",
            name="uq_survey_response_effect_response_type",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "effect_type",
            "scope_key",
            name="uq_survey_response_effect_tenant_type_scope",
        ),
    )
    op.create_index(
        "ix_survey_response_effect_due",
        "survey_response_effect_outbox",
        ["status", "available_at", "leased_until"],
        unique=False,
    )
    op.create_index(
        "ix_survey_response_effect_tenant_due",
        "survey_response_effect_outbox",
        ["tenant_id", "status", "available_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_survey_response_effect_tenant_due",
        table_name="survey_response_effect_outbox",
    )
    op.drop_index(
        "ix_survey_response_effect_due",
        table_name="survey_response_effect_outbox",
    )
    op.drop_table("survey_response_effect_outbox")

    op.drop_index(
        "uq_points_transaction_tenant_idempotency",
        table_name="points_transaction",
    )
    op.drop_column("points_transaction", "idempotency_key")
