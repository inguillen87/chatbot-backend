"""add canonical tenant-scoped channel session identity v1

Revision ID: 20260730_channel_session_identity_v1
Revises: 20260730_interview_consent_text_v1
Create Date: 2026-07-30 06:25:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_channel_session_identity_v1"
down_revision = "20260730_interview_consent_text_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_session_identity_binding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("identity_version", sa.String(length=32), nullable=False),
        sa.Column("identity_hmac", sa.String(length=64), nullable=False),
        sa.Column("chat_session_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("continuity_status", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_conflict_code", sa.String(length=64), nullable=True),
        sa.Column("previous_session_digest", sa.String(length=64), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="channel.session_identity.v1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chat_session_id"],
            ["chat_session_context.chat_session_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_channel_session_identity_binding_id_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "provider",
            "identity_version",
            "identity_hmac",
            name="uq_channel_session_identity_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active','quarantined')",
            name="ck_channel_session_identity_status",
        ),
        sa.CheckConstraint(
            "continuity_status IN ('new','adopted','isolated')",
            name="ck_channel_session_identity_continuity",
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_channel_session_identity_generation",
        ),
        sa.CheckConstraint(
            "length(identity_hmac) = 64",
            name="ck_channel_session_identity_hmac_length",
        ),
        sa.CheckConstraint(
            "previous_session_digest IS NULL OR length(previous_session_digest) = 64",
            name="ck_channel_session_identity_previous_digest",
        ),
    )
    op.create_index(
        "ix_channel_session_identity_binding_tenant_id",
        "channel_session_identity_binding",
        ["tenant_id"],
    )
    op.create_index(
        "ix_channel_session_identity_binding_chat_session_id",
        "channel_session_identity_binding",
        ["chat_session_id"],
    )
    op.create_index(
        "ix_channel_session_identity_lookup",
        "channel_session_identity_binding",
        ["tenant_id", "channel", "provider", "identity_hmac"],
    )

    with op.batch_alter_table("whatsapp_inbound_turn") as batch_op:
        batch_op.add_column(
            sa.Column("session_identity_binding_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("session_identity_version", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("session_identity_hmac", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_whatsapp_inbound_turn_session_identity_tenant",
            "channel_session_identity_binding",
            ["session_identity_binding_id", "tenant_id"],
            ["id", "tenant_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_whatsapp_inbound_turn_session_identity_complete",
            "((session_identity_binding_id IS NULL "
            "AND session_identity_version IS NULL "
            "AND session_identity_hmac IS NULL) OR "
            "(session_identity_binding_id IS NOT NULL "
            "AND session_identity_version IS NOT NULL "
            "AND session_identity_hmac IS NOT NULL "
            "AND length(session_identity_hmac) = 64))",
        )
    op.create_index(
        "ix_whatsapp_inbound_turn_session_identity",
        "whatsapp_inbound_turn",
        ["tenant_id", "session_identity_binding_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_inbound_turn_session_identity",
        table_name="whatsapp_inbound_turn",
    )
    with op.batch_alter_table("whatsapp_inbound_turn") as batch_op:
        batch_op.drop_constraint(
            "ck_whatsapp_inbound_turn_session_identity_complete",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_whatsapp_inbound_turn_session_identity_tenant",
            type_="foreignkey",
        )
        batch_op.drop_column("session_identity_hmac")
        batch_op.drop_column("session_identity_version")
        batch_op.drop_column("session_identity_binding_id")

    op.drop_index(
        "ix_channel_session_identity_lookup",
        table_name="channel_session_identity_binding",
    )
    op.drop_index(
        "ix_channel_session_identity_binding_chat_session_id",
        table_name="channel_session_identity_binding",
    )
    op.drop_index(
        "ix_channel_session_identity_binding_tenant_id",
        table_name="channel_session_identity_binding",
    )
    op.drop_table("channel_session_identity_binding")
