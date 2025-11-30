"""add tenant to chat session context

Revision ID: 21abffd07675
Revises: 20261012_add_marketplace_cart_order
Create Date: 2025-11-30 07:54:37.496296

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '21abffd07675'
down_revision = '20261012_add_marketplace_cart_order'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_session_context",
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_chat_session_context_tenant_id"),
        "chat_session_context",
        ["tenant_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_chat_session_context_tenant_id",
        "chat_session_context",
        "tenant_profile",
        ["tenant_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint("fk_chat_session_context_tenant_id", "chat_session_context", type_="foreignkey")
    op.drop_index(op.f("ix_chat_session_context_tenant_id"), table_name="chat_session_context")
    op.drop_column("chat_session_context", "tenant_id")
