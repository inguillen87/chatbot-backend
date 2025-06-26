"""Add logo_url and color columns to User

Revision ID: aa1234567890
Revises: ee3694e2e48a
Create Date: 2025-06-26 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'aa1234567890'
down_revision = 'ee3694e2e48a'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('user', sa.Column('logo_url', sa.String(length=255), nullable=True))
    op.add_column('user', sa.Column('color_primario', sa.String(length=20), nullable=True))
    op.add_column('user', sa.Column('color_secundario', sa.String(length=20), nullable=True))
    op.add_column('user', sa.Column('badge_tipo', sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column('user', 'badge_tipo')
    op.drop_column('user', 'color_secundario')
    op.drop_column('user', 'color_primario')
    op.drop_column('user', 'logo_url')
