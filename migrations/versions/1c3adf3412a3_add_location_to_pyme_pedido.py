"""add location columns to pyme_pedido

Revision ID: 1c3adf3412a3
Revises: 61f1e2b3bdc5
Create Date: 2025-07-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '1c3adf3412a3'
down_revision = '61f1e2b3bdc5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('pyme_pedido', schema=None) as batch_op:
        batch_op.add_column(sa.Column('direccion', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('latitud', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('longitud', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('pyme_pedido', schema=None) as batch_op:
        batch_op.drop_column('longitud')
        batch_op.drop_column('latitud')
        batch_op.drop_column('direccion')

