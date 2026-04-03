"""BE-01 conversation core tables and chat_session compatibility

Revision ID: 20260331_be01_conversation_core
Revises: 20300119_merge_catalog_and_order_event_heads
Create Date: 2026-03-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260331_be01_conversation_core"
down_revision = "20300119_merge_catalog_and_order_event_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("legacy_chat_session_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "legacy_chat_session_id", name="uq_conversation_tenant_legacy_chat_session"),
    )
    op.create_index("ix_conversation_tenant_id", "conversation", ["tenant_id"])
    op.create_index("ix_conversation_legacy_chat_session_id", "conversation", ["legacy_chat_session_id"])

    op.create_table(
        "channel_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("channel_identity", sa.String(length=120), nullable=True),
        sa.Column("chat_session_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_channel_session_conversation_id", "channel_session", ["conversation_id"])
    op.create_index("ix_channel_session_tenant_id", "channel_session", ["tenant_id"])
    op.create_index("ix_channel_session_chat_session_id", "channel_session", ["chat_session_id"])

    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("channel_session_id", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("sender_type", sa.String(length=20), nullable=False),
        sa.Column("sender_user_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_session_id"], ["channel_session.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
        sa.ForeignKeyConstraint(["sender_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])
    op.create_index("ix_message_channel_session_id", "message", ["channel_session_id"])
    op.create_index("ix_message_tenant_id", "message", ["tenant_id"])
    op.create_index("ix_message_created_at", "message", ["created_at"])

    op.add_column("chat_session_context", sa.Column("conversation_id", sa.String(length=36), nullable=True))
    op.add_column("chat_session_context", sa.Column("channel_session_id", sa.Integer(), nullable=True))
    op.create_index("ix_chat_session_context_conversation_id", "chat_session_context", ["conversation_id"])
    op.create_index("ix_chat_session_context_channel_session_id", "chat_session_context", ["channel_session_id"])
    op.create_foreign_key(
        "fk_chat_session_context_conversation_id",
        "chat_session_context",
        "conversation",
        ["conversation_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_chat_session_context_channel_session_id",
        "chat_session_context",
        "channel_session",
        ["channel_session_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint("fk_chat_session_context_channel_session_id", "chat_session_context", type_="foreignkey")
    op.drop_constraint("fk_chat_session_context_conversation_id", "chat_session_context", type_="foreignkey")
    op.drop_index("ix_chat_session_context_channel_session_id", table_name="chat_session_context")
    op.drop_index("ix_chat_session_context_conversation_id", table_name="chat_session_context")
    op.drop_column("chat_session_context", "channel_session_id")
    op.drop_column("chat_session_context", "conversation_id")

    op.drop_index("ix_message_created_at", table_name="message")
    op.drop_index("ix_message_tenant_id", table_name="message")
    op.drop_index("ix_message_channel_session_id", table_name="message")
    op.drop_index("ix_message_conversation_id", table_name="message")
    op.drop_table("message")

    op.drop_index("ix_channel_session_chat_session_id", table_name="channel_session")
    op.drop_index("ix_channel_session_tenant_id", table_name="channel_session")
    op.drop_index("ix_channel_session_conversation_id", table_name="channel_session")
    op.drop_table("channel_session")

    op.drop_index("ix_conversation_legacy_chat_session_id", table_name="conversation")
    op.drop_index("ix_conversation_tenant_id", table_name="conversation")
    op.drop_table("conversation")
