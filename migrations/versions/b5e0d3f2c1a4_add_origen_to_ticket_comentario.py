"""Add origen column to ticket_comentario

Revision ID: b5e0d3f2c1a4
Revises: a1b2c3d4e5f6
Create Date: 2025-08-14 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b5e0d3f2c1a4'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('ticket_comentario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origen', sa.String(length=20), nullable=True, server_default='chat'))


def downgrade():
    with op.batch_alter_table('ticket_comentario', schema=None) as batch_op:
        batch_op.drop_column('origen')
