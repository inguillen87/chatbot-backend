"""add direccion columns to tickets

Revision ID: 61f1e2b3bdc5
Revises: e1e8d4a4968d
Create Date: 2025-06-22 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '61f1e2b3bdc5'
down_revision = 'e1e8d4a4968d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('direccion', sa.String(length=255), nullable=True))

    with op.batch_alter_table('pyme_ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('direccion', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('pyme_ticket', schema=None) as batch_op:
        batch_op.drop_column('direccion')

    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('direccion')
