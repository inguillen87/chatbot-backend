"""add generic webhook delivery receipts

Revision ID: 20260713_webhook_delivery
Revises: 20260712_pyme_pedido_idem
Create Date: 2026-07-13 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_webhook_delivery"
down_revision = "20260712_pyme_pedido_idem"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'processing'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
            "attempts >= 1",
            name="ck_webhook_delivery_attempts_positive",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'processed', 'failed')",
            name="ck_webhook_delivery_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "event_id",
            name="uq_webhook_delivery_provider_event",
        ),
    )
    op.create_index(
        "ix_webhook_delivery_status_updated_at",
        "webhook_delivery",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_delivery_status_updated_at",
        table_name="webhook_delivery",
    )
    op.drop_table("webhook_delivery")
