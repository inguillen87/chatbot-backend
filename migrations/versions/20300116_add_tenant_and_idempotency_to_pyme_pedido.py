"""Add tenant_id and idempotency_key to PymePedido

Revision ID: 20300116_add_tenant_and_idempotency
Revises: 20300115_add_dispatch_fields_to_tenant
Create Date: 2030-01-16 12:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300116_add_tenant_and_idempotency'
down_revision = '20300115_add_dispatch_fields_to_tenant'
branch_labels = None
depends_on = None

def upgrade():
    # Add tenant_id column
    with op.batch_alter_table('pyme_pedido', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('idempotency_key', sa.String(length=128), nullable=True))
        batch_op.create_index(batch_op.f('ix_pyme_pedido_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pyme_pedido_idempotency_key'), ['idempotency_key'], unique=True)
        batch_op.create_foreign_key('fk_pyme_pedido_tenant_id', 'tenant_profile', ['tenant_id'], ['id'])

def downgrade():
    with op.batch_alter_table('pyme_pedido', schema=None) as batch_op:
        batch_op.drop_constraint('fk_pyme_pedido_tenant_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_pyme_pedido_idempotency_key'))
        batch_op.drop_index(batch_op.f('ix_pyme_pedido_tenant_id'))
        batch_op.drop_column('idempotency_key')
        batch_op.drop_column('tenant_id')
