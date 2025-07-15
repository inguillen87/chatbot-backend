"""Agrego campos de vecino a municipio_ticket

Revision ID: 4b9953f012b9
Revises: db48e3a4c174
Create Date: 2025-07-09 13:28:18.669144

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '4b9953f012b9'
down_revision = 'db48e3a4c174'
branch_labels = None
depends_on = None

def upgrade():
   # with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
    #    batch_op.add_column(sa.Column('nombre_vecino', sa.String(length=150), nullable=True))
    #with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
     #   batch_op.add_column(sa.Column('telefono_vecino', sa.String(length=30), nullable=True))
    #with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
     #   batch_op.add_column(sa.Column('email_vecino', sa.String(length=120), nullable=True))
    #with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
     #   batch_op.add_column(sa.Column('foto_url_directa', sa.String(length=255), nullable=True))
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('archivo_url')

def downgrade():
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archivo_url', sa.VARCHAR(length=255), nullable=True))
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('foto_url_directa')
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('email_vecino')
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('telefono_vecino')
    with op.batch_alter_table('municipio_ticket', schema=None) as batch_op:
        batch_op.drop_column('nombre_vecino')
