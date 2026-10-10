"""add tenant scoped inbox ticket artifacts

Revision ID: 20260831_inbox_artifact_v1
Revises: 20260830_geo_sync_v1
Create Date: 2026-08-31 14:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_inbox_artifact_v1"
down_revision = "20260830_geo_sync_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_ticket_artifact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("source_model", sa.String(length=32), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=48), server_default="inbox.ticket_artifact.v1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_model IN ('TenantTicket', 'MunicipioTicket')", name="ck_inbox_ticket_artifact_source_model"),
        sa.CheckConstraint("action IN ('attach_file', 'share_location', 'send_form')", name="ck_inbox_ticket_artifact_action"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_model", "ticket_id", "idempotency_key_hash", name="uq_inbox_ticket_artifact_idempotency"),
    )
    op.create_index(
        "ix_inbox_ticket_artifact_ticket", "inbox_ticket_artifact",
        ["tenant_id", "source_model", "ticket_id", "created_at", "id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_ticket_artifact_ticket", table_name="inbox_ticket_artifact")
    op.drop_table("inbox_ticket_artifact")
