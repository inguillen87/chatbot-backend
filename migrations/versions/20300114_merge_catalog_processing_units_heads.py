"""Merge catalog processing and pallet unit heads.

Revision ID: 20300114_merge_catalog_processing_units_heads
Revises: 20300113, 8e8cabaf31c8
Create Date: 2030-01-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300114_merge_catalog_processing_units_heads'
down_revision = ('20300113', '8e8cabaf31c8')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
