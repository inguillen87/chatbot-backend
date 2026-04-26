"""Add metadata column to catalogo_item

Revision ID: 20260622_add_metadata_to_catalogo_item
Revises: 20260621_add_modalidad_to_catalogo_item
Create Date: 2026-06-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260622_add_metadata_to_catalogo_item"
down_revision = "20260621_add_modalidad_to_catalogo_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalogo_item", sa.Column("metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("catalogo_item", "metadata")
