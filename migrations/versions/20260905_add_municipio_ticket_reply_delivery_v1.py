"""add durable municipal WhatsApp reply delivery

Revision ID: 20260905_municipio_reply_v1
Revises: 20260905_government_launch_v1
Create Date: 2026-09-05 18:00:00.000000

The legacy municipal timeline remains in ``ticket_comentario``.  This table
pins the immutable delivery identity required by the tenant-scoped outbox and
signed provider-status callback.
"""

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision = "20260905_municipio_reply_v1"
down_revision = "20260905_government_launch_v1"
branch_labels = None
depends_on = None


_JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "municipio_ticket_reply_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "source_model",
            sa.String(length=32),
            server_default="MunicipioTicket",
            nullable=False,
        ),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=16),
            server_default="public",
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("recipient_phone", sa.String(length=64), nullable=False),
        sa.Column("whatsapp_template_registry_id", sa.Integer(), nullable=True),
        sa.Column("whatsapp_template_variables", _JSON_TYPE, nullable=True),
        sa.Column("whatsapp_policy_snapshot", _JSON_TYPE, nullable=False),
        sa.Column(
            "whatsapp_delivery_status",
            sa.String(length=24),
            server_default="saved",
            nullable=False,
        ),
        sa.Column("whatsapp_provider_message_id", sa.String(length=180), nullable=True),
        sa.Column("whatsapp_provider_sender_id", sa.Integer(), nullable=False),
        sa.Column("whatsapp_provider_status", sa.String(length=80), nullable=True),
        sa.Column("whatsapp_error_code", sa.String(length=80), nullable=True),
        sa.Column("whatsapp_status_event_id", sa.Integer(), nullable=True),
        sa.Column("whatsapp_status_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_provider_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            server_default="municipio_ticket.reply_event.v1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_model = 'MunicipioTicket'",
            name="ck_municipio_reply_source_model",
        ),
        sa.CheckConstraint(
            "visibility = 'public'",
            name="ck_municipio_reply_public_visibility",
        ),
        sa.CheckConstraint(
            "length(trim(body)) > 0",
            name="ck_municipio_reply_body_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(event_id)) > 0",
            name="ck_municipio_reply_event_id_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(recipient_phone)) > 0",
            name="ck_municipio_reply_recipient_nonempty",
        ),
        sa.CheckConstraint(
            "whatsapp_delivery_status IN ('saved', 'queued', 'uncertain', "
            "'provider_accepted', 'delivered', 'read', 'failed')",
            name="ck_municipio_reply_wa_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["municipio_ticket.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"], ["ticket_comentario.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["whatsapp_template_registry_id"],
            ["message_template_registry.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["whatsapp_provider_sender_id"],
            ["provider_sender.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["whatsapp_status_event_id"],
            ["messaging_event_ledger.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_municipio_reply_tenant_event",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "comment_id",
            name="uq_municipio_reply_tenant_comment",
        ),
    )
    op.create_index(
        "ix_municipio_reply_ticket",
        "municipio_ticket_reply_event",
        ["tenant_id", "ticket_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_municipio_reply_wa_provider_message",
        "municipio_ticket_reply_event",
        ["tenant_id", "whatsapp_provider_message_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_municipio_reply_wa_provider_message",
        table_name="municipio_ticket_reply_event",
    )
    op.drop_index(
        "ix_municipio_reply_ticket",
        table_name="municipio_ticket_reply_event",
    )
    op.drop_table("municipio_ticket_reply_event")
