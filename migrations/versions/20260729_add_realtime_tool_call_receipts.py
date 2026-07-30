"""add durable Realtime tool-call receipts

Revision ID: 20260729_realtime_tool_receipts
Revises: 20260729_ticket_effects
Create Date: 2026-07-29 14:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_realtime_tool_receipts"
down_revision = "20260729_ticket_effects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "realtime_tool_call_receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("session_id_hash", sa.String(length=64), nullable=False),
        sa.Column("call_id_hash", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("effect_idempotency_key", sa.String(length=191), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="reserved",
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="realtime.tool_call.v1",
        ),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('reserved', 'completed', 'unknown')",
            name="ck_realtime_tool_receipt_status",
        ),
        sa.CheckConstraint(
            "length(session_id_hash) = 64",
            name="ck_realtime_tool_receipt_session_hash",
        ),
        sa.CheckConstraint(
            "length(call_id_hash) = 64",
            name="ck_realtime_tool_receipt_call_hash",
        ),
        sa.CheckConstraint(
            "length(arguments_hash) = 64",
            name="ck_realtime_tool_receipt_args_hash",
        ),
        sa.CheckConstraint(
            "output_text IS NULL OR length(output_text) <= 4096",
            name="ck_realtime_tool_receipt_output_size",
        ),
        sa.CheckConstraint(
            "((status = 'reserved' AND output_text IS NULL AND completed_at IS NULL) "
            "OR (status IN ('completed', 'unknown') AND output_text IS NOT NULL "
            "AND completed_at IS NOT NULL))",
            name="ck_realtime_tool_receipt_terminal_state",
        ),
        sa.CheckConstraint(
            "(status <> 'unknown' OR last_error_code IS NOT NULL)",
            name="ck_realtime_tool_receipt_unknown_error",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id_hash",
            "call_id_hash",
            name="uq_realtime_tool_receipt_scope_call",
        ),
    )
    op.create_index(
        "ix_realtime_tool_receipt_status_updated",
        "realtime_tool_call_receipt",
        ["status", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_realtime_tool_receipt_tenant_tool",
        "realtime_tool_call_receipt",
        ["tenant_id", "tool_name", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_realtime_tool_receipt_tenant_tool",
        table_name="realtime_tool_call_receipt",
    )
    op.drop_index(
        "ix_realtime_tool_receipt_status_updated",
        table_name="realtime_tool_call_receipt",
    )
    op.drop_table("realtime_tool_call_receipt")
