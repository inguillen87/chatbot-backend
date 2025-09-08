"""add accessibility field to user

Revision ID: 3f3b6a9b7d6e
Revises: f272cf2b197d
Create Date: 2025-09-08 15:10:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '3f3b6a9b7d6e'
down_revision = 'f272cf2b197d'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user', sa.Column('accesibilidad', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('user', 'accesibilidad')
