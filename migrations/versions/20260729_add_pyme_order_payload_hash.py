"""bind PymePedido idempotency keys to canonical payload hashes

Revision ID: 20260729_pyme_order_payload_hash
Revises: 20260729_realtime_tool_receipts
Create Date: 2026-07-29 22:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_pyme_order_payload_hash"
down_revision = "20260729_realtime_tool_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pyme_pedido", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("idempotency_payload_hash", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_pyme_pedido_idempotency_payload_hash",
            "idempotency_payload_hash IS NULL OR length(idempotency_payload_hash) = 64",
        )
        batch_op.create_check_constraint(
            "ck_pyme_pedido_payload_hash_requires_key",
            "idempotency_payload_hash IS NULL OR idempotency_key IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("pyme_pedido", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_pyme_pedido_payload_hash_requires_key",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_pyme_pedido_idempotency_payload_hash",
            type_="check",
        )
        batch_op.drop_column("idempotency_payload_hash")
