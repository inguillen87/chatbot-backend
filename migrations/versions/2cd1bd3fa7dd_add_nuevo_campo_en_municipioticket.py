"""Add nuevo campo en MunicipioTicket

Revision ID: 2cd1bd3fa7dd
Revises: ce8254522e14
Create Date: 2025-08-27 11:47:52.667910

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2cd1bd3fa7dd'
down_revision = 'ce8254522e14'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dni_vecino', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('dni_vecino')
