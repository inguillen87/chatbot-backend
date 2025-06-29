"""Merge heads para unificar migraciones

Revision ID: ac249b67a8f9
Revises: abcdef123456, c9c5e5bfdafb
Create Date: 2025-06-29 14:18:40.272175

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ac249b67a8f9'
down_revision = ('abcdef123456', 'c9c5e5bfdafb')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
