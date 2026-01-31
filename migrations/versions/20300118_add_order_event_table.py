"""Add OrderEvent model

Revision ID: 20300118_add_order_event_table
Revises: 20300117_add_notification_toggles
Create Date: 2030-01-18 10:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300118_add_order_event_table'
down_revision = '20300117_add_notification_toggles'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('order_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pyme_pedido_id', sa.Integer(), nullable=True),
    sa.Column('market_order_id', sa.Integer(), nullable=True),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['market_order_id'], ['market_order.id'], ),
    sa.ForeignKeyConstraint(['pyme_pedido_id'], ['pyme_pedido.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('order_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_order_event_market_order_id'), ['market_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_order_event_pyme_pedido_id'), ['pyme_pedido_id'], unique=False)

def downgrade():
    op.drop_table('order_event')
