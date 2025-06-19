"""add rol and marketing fields to user

Revision ID: c345d6789eab
Revises: b123a1b2c3d4
Create Date: 2025-06-20 00:00:01
"""
from alembic import op
import sqlalchemy as sa

revision = 'c345d6789eab'
down_revision = 'b123a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rol', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('acepta_marketing', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('fecha_aceptacion_marketing', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('fecha_aceptacion_marketing')
        batch_op.drop_column('acepta_marketing')
        batch_op.drop_column('rol')
