"""add empresa_id to user

Revision ID: b123a1b2c3d4
Revises: e1e8d4a4968d
Create Date: 2025-06-20 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'b123a1b2c3d4'
down_revision = 'e1e8d4a4968d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('empresa_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_user_empresa', 'user', ['empresa_id'], ['id'])


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('fk_user_empresa', type_='foreignkey')
        batch_op.drop_column('empresa_id')
