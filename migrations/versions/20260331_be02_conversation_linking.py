"""BE-02 conversation linking via OTP/deep-link

Revision ID: 20260331_be02_conversation_linking
Revises: 20260331_be01_conversation_core
Create Date: 2026-03-31 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260331_be02_conversation_linking"
down_revision = "20260331_be01_conversation_core"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation_link_request",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("source_channel_session_id", sa.Integer(), nullable=True),
        sa.Column("target_channel", sa.String(length=20), nullable=False),
        sa.Column("target_identity", sa.String(length=120), nullable=False),
        sa.Column("otp_code", sa.String(length=255), nullable=False),
        sa.Column("deep_link_token", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["source_channel_session_id"], ["channel_session.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deep_link_token"),
    )
    op.create_index("ix_conversation_link_request_tenant_id", "conversation_link_request", ["tenant_id"])
    op.create_index("ix_conversation_link_request_conversation_id", "conversation_link_request", ["conversation_id"])
    op.create_index("ix_conversation_link_request_source_channel_session_id", "conversation_link_request", ["source_channel_session_id"])
    op.create_index("ix_conversation_link_request_target_identity", "conversation_link_request", ["target_identity"])
    op.create_index("ix_conversation_link_request_deep_link_token", "conversation_link_request", ["deep_link_token"])
    op.create_index("ix_conversation_link_request_status", "conversation_link_request", ["status"])
    op.create_index("ix_conversation_link_request_expires_at", "conversation_link_request", ["expires_at"])


def downgrade():
    op.drop_index("ix_conversation_link_request_expires_at", table_name="conversation_link_request")
    op.drop_index("ix_conversation_link_request_status", table_name="conversation_link_request")
    op.drop_index("ix_conversation_link_request_deep_link_token", table_name="conversation_link_request")
    op.drop_index("ix_conversation_link_request_target_identity", table_name="conversation_link_request")
    op.drop_index("ix_conversation_link_request_source_channel_session_id", table_name="conversation_link_request")
    op.drop_index("ix_conversation_link_request_conversation_id", table_name="conversation_link_request")
    op.drop_index("ix_conversation_link_request_tenant_id", table_name="conversation_link_request")
    op.drop_table("conversation_link_request")
