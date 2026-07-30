"""persist PymePedido business context for delayed workers

Revision ID: 20260730_pyme_order_context
Revises: 20260729_domain_effect_outbox
Create Date: 2026-07-30 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_pyme_order_context"
down_revision = "20260729_domain_effect_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pyme_pedido", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rubro", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("channel", sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pyme_pedido", schema=None) as batch_op:
        batch_op.drop_column("channel")
        batch_op.drop_column("rubro")
