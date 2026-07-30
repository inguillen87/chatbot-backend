"""add durable WhatsApp inbound turns and outbound attempts

Revision ID: 20260729_whatsapp_turns
Revises: 20260728_survey_effect_outbox
Create Date: 2026-07-29 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260729_whatsapp_turns"
down_revision = "20260728_survey_effect_outbox"
branch_labels = None
depends_on = None


def _json_type():
    bind = op.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "whatsapp_inbound_turn",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=True),
        sa.Column("provider_sender_id", sa.Integer(), nullable=True),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="twilio",
        ),
        sa.Column("provider_message_sid", sa.String(length=180), nullable=False),
        sa.Column("stream_key", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("channel_session_id", sa.Integer(), nullable=True),
        sa.Column("chat_session_id", sa.String(length=64), nullable=True),
        sa.Column(
            "message_kind",
            sa.String(length=24),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", json_type, nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="received",
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
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("result_json", json_type, nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="whatsapp.inbound_turn.v1",
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
            "status IN ('received', 'processing', 'retry_wait', 'completed', 'dead')",
            name="ck_whatsapp_inbound_turn_status",
        ),
        sa.CheckConstraint(
            "message_kind IN ('text', 'audio', 'image', 'video', 'document', "
            "'location', 'contact', 'interactive', 'flow', 'unknown')",
            name="ck_whatsapp_inbound_turn_message_kind",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_whatsapp_inbound_turn_attempts",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name="ck_whatsapp_inbound_turn_payload_digest",
        ),
        sa.CheckConstraint(
            "((status = 'processing' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) "
            "OR (status <> 'processing' AND lease_token IS NULL AND leased_until IS NULL))",
            name="ck_whatsapp_inbound_turn_lease_state",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('completed', 'dead') OR completed_at IS NOT NULL)",
            name="ck_whatsapp_inbound_turn_completion_time",
        ),
        sa.ForeignKeyConstraint(
            ["provider_connection_id"],
            ["provider_connection.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_sender_id"],
            ["provider_sender.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", name="uq_whatsapp_inbound_turn_turn_id"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_whatsapp_inbound_turn_id_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_message_sid",
            name="uq_whatsapp_inbound_turn_tenant_provider_message",
        ),
    )
    op.create_index(
        "ix_whatsapp_inbound_turn_due",
        "whatsapp_inbound_turn",
        ["status", "available_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_inbound_turn_stream_fifo",
        "whatsapp_inbound_turn",
        ["tenant_id", "stream_key", "id"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_inbound_turn_stale",
        "whatsapp_inbound_turn",
        ["status", "leased_until"],
        unique=False,
    )
    op.create_index(
        "uq_whatsapp_inbound_turn_stream_processing",
        "whatsapp_inbound_turn",
        ["tenant_id", "stream_key"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
        sqlite_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "whatsapp_outbound_attempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("inbound_turn_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=True),
        sa.Column("provider_sender_id", sa.Integer(), nullable=True),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="twilio",
        ),
        sa.Column("stream_key", sa.String(length=128), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("message_kind", sa.String(length=24), nullable=False),
        sa.Column("idempotency_key", sa.String(length=191), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", json_type, nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("provider_message_sid", sa.String(length=180), nullable=True),
        sa.Column(
            "provider_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
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
            server_default="5",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=96), nullable=True),
        sa.Column("last_error_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="whatsapp.outbound_attempt.v1",
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
            "status IN ('pending', 'sending', 'retry_wait', 'send_uncertain', "
            "'accepted', 'failed', 'dead', 'cancelled')",
            name="ck_whatsapp_outbound_attempt_status",
        ),
        sa.CheckConstraint(
            "provider_status IN ('unknown', 'accepted', 'scheduled', 'queued', "
            "'sending', 'sent', 'delivered', 'read', 'failed', 'undelivered', "
            "'canceled', 'cancelled')",
            name="ck_whatsapp_outbound_attempt_provider_status",
        ),
        sa.CheckConstraint(
            "message_kind IN ('text', 'media', 'audio', 'template', 'interactive')",
            name="ck_whatsapp_outbound_attempt_message_kind",
        ),
        sa.CheckConstraint(
            "sequence_no >= 1",
            name="ck_whatsapp_outbound_attempt_sequence_positive",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_whatsapp_outbound_attempt_attempts",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64",
            name="ck_whatsapp_outbound_attempt_payload_digest",
        ),
        sa.CheckConstraint(
            "((status = 'sending' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) "
            "OR (status <> 'sending' AND lease_token IS NULL AND leased_until IS NULL))",
            name="ck_whatsapp_outbound_attempt_lease_state",
        ),
        sa.CheckConstraint(
            "(status <> 'accepted' OR provider_message_sid IS NOT NULL)",
            name="ck_whatsapp_outbound_attempt_accepted_sid",
        ),
        sa.ForeignKeyConstraint(
            ["inbound_turn_id", "tenant_id"],
            ["whatsapp_inbound_turn.id", "whatsapp_inbound_turn.tenant_id"],
            name="fk_whatsapp_outbound_attempt_turn_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_connection_id"],
            ["provider_connection.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_sender_id"],
            ["provider_sender.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_whatsapp_outbound_attempt_attempt_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_whatsapp_outbound_attempt_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "inbound_turn_id",
            "sequence_no",
            name="uq_whatsapp_outbound_attempt_turn_sequence",
        ),
    )
    op.create_index(
        "ix_whatsapp_outbound_attempt_due",
        "whatsapp_outbound_attempt",
        ["status", "available_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_outbound_attempt_stream_fifo",
        "whatsapp_outbound_attempt",
        ["tenant_id", "stream_key", "inbound_turn_id", "sequence_no"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_outbound_attempt_callback",
        "whatsapp_outbound_attempt",
        ["tenant_id", "provider_sender_id", "provider_message_sid"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_outbound_attempt_uncertain",
        "whatsapp_outbound_attempt",
        ["tenant_id", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "uq_whatsapp_outbound_attempt_provider_message",
        "whatsapp_outbound_attempt",
        ["tenant_id", "provider", "provider_message_sid"],
        unique=True,
        postgresql_where=sa.text("provider_message_sid IS NOT NULL"),
        sqlite_where=sa.text("provider_message_sid IS NOT NULL"),
    )
    op.create_index(
        "uq_whatsapp_outbound_attempt_stream_sending",
        "whatsapp_outbound_attempt",
        ["tenant_id", "stream_key"],
        unique=True,
        postgresql_where=sa.text("status = 'sending'"),
        sqlite_where=sa.text("status = 'sending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_whatsapp_outbound_attempt_stream_sending",
        table_name="whatsapp_outbound_attempt",
    )
    op.drop_index(
        "uq_whatsapp_outbound_attempt_provider_message",
        table_name="whatsapp_outbound_attempt",
    )
    op.drop_index(
        "ix_whatsapp_outbound_attempt_uncertain",
        table_name="whatsapp_outbound_attempt",
    )
    op.drop_index(
        "ix_whatsapp_outbound_attempt_callback",
        table_name="whatsapp_outbound_attempt",
    )
    op.drop_index(
        "ix_whatsapp_outbound_attempt_stream_fifo",
        table_name="whatsapp_outbound_attempt",
    )
    op.drop_index(
        "ix_whatsapp_outbound_attempt_due",
        table_name="whatsapp_outbound_attempt",
    )
    op.drop_table("whatsapp_outbound_attempt")

    op.drop_index(
        "uq_whatsapp_inbound_turn_stream_processing",
        table_name="whatsapp_inbound_turn",
    )
    op.drop_index(
        "ix_whatsapp_inbound_turn_stale",
        table_name="whatsapp_inbound_turn",
    )
    op.drop_index(
        "ix_whatsapp_inbound_turn_stream_fifo",
        table_name="whatsapp_inbound_turn",
    )
    op.drop_index(
        "ix_whatsapp_inbound_turn_due",
        table_name="whatsapp_inbound_turn",
    )
    op.drop_table("whatsapp_inbound_turn")
