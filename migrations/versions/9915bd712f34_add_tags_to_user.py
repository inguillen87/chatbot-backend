"""add tags column to user

Revision ID: 9915bd712f34
Revises: 47d4436707cd
Create Date: 2025-06-22 00:00:00
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '9915bd712f34'
down_revision = '47d4436707cd'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tags', sa.String(length=255), nullable=True, server_default=""))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('tags')

