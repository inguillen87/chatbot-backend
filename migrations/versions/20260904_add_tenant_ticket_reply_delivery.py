"""add durable WhatsApp delivery lifecycle to TenantTicket replies

Revision ID: 20260904_tenant_reply_delivery_v1
Revises: 20260831_inbox_artifact_v1
Create Date: 2026-09-04

This forward-only migration preserves provider and ambiguity evidence.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_tenant_reply_delivery_v1"
down_revision = "20260831_inbox_artifact_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_ticket_reply_event") as batch_op:
        batch_op.add_column(sa.Column("whatsapp_template_registry_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_template_variables", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_policy_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "whatsapp_delivery_status",
                sa.String(length=24),
                server_default="saved",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("whatsapp_provider_message_id", sa.String(length=180), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_provider_sender_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_provider_status", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_error_code", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_status_event_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_status_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_provider_accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_delivered_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_read_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("whatsapp_failed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_ttre_wa_template",
            "message_template_registry",
            ["whatsapp_template_registry_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_ttre_wa_sender",
            "provider_sender",
            ["whatsapp_provider_sender_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_ttre_wa_status_event",
            "messaging_event_ledger",
            ["whatsapp_status_event_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_tenant_ticket_reply_event_wa_delivery_status",
            "whatsapp_delivery_status IN ('saved', 'queued', "
            "'uncertain', 'provider_accepted', 'delivered', 'read', 'failed')",
        )
        batch_op.create_index(
            "ix_tenant_ticket_reply_event_wa_provider_message",
            ["tenant_id", "whatsapp_provider_message_id"],
            unique=True,
        )

    with op.batch_alter_table("whatsapp_contact_state") as batch_op:
        batch_op.add_column(sa.Column("provider_sender_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_whatsapp_contact_state_provider_sender",
            "provider_sender",
            ["provider_sender_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint(
            "uq_whatsapp_contact_state_tenant_recipient",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_whatsapp_contact_state_tenant_sender_recipient",
            ["tenant_id", "provider_sender_id", "recipient"],
        )
        batch_op.create_index(
            "ix_whatsapp_contact_state_provider_sender_id",
            ["provider_sender_id"],
            unique=False,
        )


def downgrade() -> None:
    raise RuntimeError(
        "20260904 tenant reply delivery is forward-only; downgrade would erase "
        "provider callback and uncertain-delivery audit evidence"
    )
