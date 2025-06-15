"""add location columns to tickets

Revision ID: e1e8d4a4968d
Revises: 2ff53327300b
Create Date: 2025-06-15 00:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e1e8d4a4968d'
down_revision = '2ff53327300b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('latitud', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('longitud', sa.Float(), nullable=True))

    with op.batch_alter_table('pyme_ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('latitud', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('longitud', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('pyme_ticket', schema=None) as batch_op:
        batch_op.drop_column('longitud')
        batch_op.drop_column('latitud')

    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('longitud')
        batch_op.drop_column('latitud')
