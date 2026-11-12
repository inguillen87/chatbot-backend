"""order WhatsApp inbound stream FIFO by original receipt time

Revision ID: 20260829_inbound_fifo_v2
Revises: 20260825_chat_idempotency_v1
Create Date: 2026-08-29 12:00:00.000000
"""

from alembic import op


revision = "20260829_inbound_fifo_v2"
down_revision = "20260825_chat_idempotency_v1"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_whatsapp_inbound_turn_stream_fifo"
TABLE_NAME = "whatsapp_inbound_turn"


def upgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ["tenant_id", "stream_key", "received_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ["tenant_id", "stream_key", "id"],
        unique=False,
    )
