"""Merge catalog processing and order event heads.

Revision ID: 20300119_merge_catalog_and_order_event_heads
Revises: 20300114_merge_catalog_processing_units_heads, 20300118_add_order_event_table
Create Date: 2030-01-19 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300119_merge_catalog_and_order_event_heads'
down_revision = ('20300114_merge_catalog_processing_units_heads', '20300118_add_order_event_table')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
