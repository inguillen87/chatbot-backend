"""add durable TenantTicket reply events

Revision ID: 20260815_tenant_reply_event_v1
Revises: 20260815_tenant_reply_receipt_v1
Create Date: 2026-08-15

TenantTicket timelines remain bounded presentation data.  This append-only
domain table is the durable source for idempotent replay and delayed provider
delivery, including the recipient snapshot selected in the reply transaction.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260815_tenant_reply_event_v1"
down_revision = "20260815_tenant_reply_receipt_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_ticket_reply_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=16),
            server_default="public",
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("recipient_email", sa.String(length=320), nullable=True),
        sa.Column("recipient_phone", sa.String(length=64), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            server_default="tenant_ticket.reply_event.v1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(body)) > 0",
            name="ck_tenant_ticket_reply_event_body_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(event_id)) > 0",
            name="ck_tenant_ticket_reply_event_id_nonempty",
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'internal')",
            name="ck_tenant_ticket_reply_event_visibility",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tenant_ticket.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_tenant_ticket_reply_event_tenant_event",
        ),
    )
    op.create_index(
        "ix_tenant_ticket_reply_event_ticket",
        "tenant_ticket_reply_event",
        ["tenant_id", "ticket_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    # Forward-only: reply text and pinned delivery evidence must survive an
    # application rollback.  The previous application version ignores it.
    pass
