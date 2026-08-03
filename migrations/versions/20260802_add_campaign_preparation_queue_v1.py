"""Add tenant-scoped campaign preparation and held queue receipts.

Revision ID: 20260802_campaign_prepare_v1
Revises: 20260802_notification_wa_v1
Create Date: 2026-08-02

This migration introduces preparation-only campaign records.  No row created
under this contract is eligible for provider dispatch: recipient receipts are
held, transport is ``not_attempted`` and attempt_count remains zero.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260802_campaign_prepare_v1"
down_revision = "20260802_notification_wa_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaign_preparation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("template_key", sa.String(length=80), nullable=False),
        sa.Column("message_template_registry_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("preview_digest", sa.String(length=64), nullable=False),
        sa.Column("rendered_subject", sa.String(length=255), nullable=True),
        sa.Column("rendered_body", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audience_json", sa.JSON(), nullable=False),
        sa.Column("readiness_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'scheduled', 'in_progress', 'completed', 'failed')",
            name="ck_campaign_preparation_status",
        ),
        sa.CheckConstraint(
            "channel IN ('whatsapp', 'email')",
            name="ck_campaign_preparation_channel",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name="ck_campaign_preparation_payload_digest",
        ),
        sa.CheckConstraint(
            "length(preview_digest) = 64",
            name="ck_campaign_preparation_preview_digest",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["notification_template.id"]),
        sa.ForeignKeyConstraint(
            ["message_template_registry_id"],
            ["message_template_registry.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_campaign_preparation_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_campaign_preparation_tenant_id",
        "campaign_preparation",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_created_by_user_id",
        "campaign_preparation",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_status",
        "campaign_preparation",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_channel",
        "campaign_preparation",
        ["channel"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_template_id",
        "campaign_preparation",
        ["template_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_message_template_registry_id",
        "campaign_preparation",
        ["message_template_registry_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_scheduled_for",
        "campaign_preparation",
        ["scheduled_for"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_preparation_tenant_created",
        "campaign_preparation",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "campaign_delivery_intent",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.String(length=36), nullable=False),
        sa.Column("queue_status", sa.String(length=20), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "transport_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_attempted",
        ),
        sa.Column("destination_digest", sa.String(length=64), nullable=True),
        sa.Column("rendered_content_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("provider_message_id", sa.String(length=180), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "queue_status IN ('held', 'excluded')",
            name="ck_campaign_delivery_intent_queue_status",
        ),
        sa.CheckConstraint(
            "transport_status IN ('not_attempted', 'unknown', 'accepted', 'sent', "
            "'delivered', 'read', 'failed')",
            name="ck_campaign_delivery_intent_transport_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_campaign_delivery_intent_attempt_count",
        ),
        sa.CheckConstraint(
            "destination_digest IS NULL OR length(destination_digest) = 64",
            name="ck_campaign_delivery_intent_destination_digest",
        ),
        sa.CheckConstraint(
            "length(rendered_content_digest) = 64",
            name="ck_campaign_delivery_intent_rendered_digest",
        ),
        sa.CheckConstraint(
            "((queue_status = 'held' AND exclusion_reason IS NULL AND "
            "destination_digest IS NOT NULL) OR "
            "(queue_status = 'excluded' AND exclusion_reason IS NOT NULL))",
            name="ck_campaign_delivery_intent_eligibility",
        ),
        sa.CheckConstraint(
            "transport_status = 'not_attempted' AND attempt_count = 0 AND "
            "provider_message_id IS NULL",
            name="ck_campaign_delivery_intent_prepare_only",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign_preparation.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contact.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "contact_id",
            name="uq_campaign_delivery_intent_contact",
        ),
    )
    op.create_index(
        "ix_campaign_delivery_intent_campaign_id",
        "campaign_delivery_intent",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_tenant_id",
        "campaign_delivery_intent",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_contact_id",
        "campaign_delivery_intent",
        ["contact_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_queue_status",
        "campaign_delivery_intent",
        ["queue_status"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_exclusion_reason",
        "campaign_delivery_intent",
        ["exclusion_reason"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_transport_status",
        "campaign_delivery_intent",
        ["transport_status"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_provider_message_id",
        "campaign_delivery_intent",
        ["provider_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_campaign_delivery_intent_tenant_queue",
        "campaign_delivery_intent",
        ["tenant_id", "queue_status", "created_at", "id"],
        unique=False,
    )


def downgrade():
    bind = op.get_bind()
    total = bind.execute(
        sa.text("SELECT COUNT(*) FROM campaign_preparation")
    ).scalar_one()
    if total:
        raise RuntimeError(
            "campaign preparation audit rows exist; downgrade would erase queue receipts"
        )

    op.drop_index(
        "ix_campaign_delivery_intent_tenant_queue",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_provider_message_id",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_transport_status",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_exclusion_reason",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_queue_status",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_contact_id",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_tenant_id",
        table_name="campaign_delivery_intent",
    )
    op.drop_index(
        "ix_campaign_delivery_intent_campaign_id",
        table_name="campaign_delivery_intent",
    )
    op.drop_table("campaign_delivery_intent")

    op.drop_index(
        "ix_campaign_preparation_tenant_created",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_scheduled_for",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_message_template_registry_id",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_template_id",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_channel",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_status",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_created_by_user_id",
        table_name="campaign_preparation",
    )
    op.drop_index(
        "ix_campaign_preparation_tenant_id",
        table_name="campaign_preparation",
    )
    op.drop_table("campaign_preparation")
