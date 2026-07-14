"""add durable WhatsApp Flow interactions

Revision ID: 20260714_wa_flow_interaction
Revises: 20260713_webhook_delivery
Create Date: 2026-07-14 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_wa_flow_interaction"
down_revision = "20260713_webhook_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_flow_interaction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("template_registry_id", sa.Integer(), nullable=False),
        sa.Column("provider_sender_id", sa.Integer(), nullable=False),
        sa.Column("flow_id", sa.String(length=120), nullable=False),
        sa.Column("meta_flow_id", sa.String(length=120), nullable=False),
        sa.Column("content_sid", sa.String(length=120), nullable=False),
        sa.Column("recipient_hash", sa.String(length=64), nullable=False),
        sa.Column("recipient_hint", sa.String(length=32), nullable=True),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="claimed"),
        sa.Column("external_message_sid", sa.String(length=180), nullable=True),
        sa.Column("inbound_message_sid", sa.String(length=180), nullable=True),
        sa.Column("data_contract", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('claimed', 'sent', 'send_uncertain', 'consumed', 'failed')",
            name="ck_whatsapp_flow_interaction_status",
        ),
        sa.ForeignKeyConstraint(["provider_sender_id"], ["provider_sender.id"]),
        sa.ForeignKeyConstraint(["template_registry_id"], ["message_template_registry.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_whatsapp_flow_interaction_token_digest"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_whatsapp_flow_interaction_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_whatsapp_flow_interaction_tenant_status",
        "whatsapp_flow_interaction",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_flow_interaction_tenant_recipient",
        "whatsapp_flow_interaction",
        ["tenant_id", "recipient_hash"],
        unique=False,
    )
    for column in (
        "tenant_id",
        "template_registry_id",
        "provider_sender_id",
        "flow_id",
        "meta_flow_id",
        "content_sid",
        "recipient_hash",
        "status",
        "external_message_sid",
        "inbound_message_sid",
        "expires_at",
        "consumed_at",
    ):
        op.create_index(
            f"ix_whatsapp_flow_interaction_{column}",
            "whatsapp_flow_interaction",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("whatsapp_flow_interaction")
