"""Add precio_puntos column to catalogo_item

Revision ID: 20260620_add_precio_puntos_to_catalogo_item
Revises: 20260615_merge_saldo_puntos_catalogo_heads
Create Date: 2026-06-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260620_add_precio_puntos_to_catalogo_item"
down_revision = "20260615_merge_saldo_puntos_catalogo_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalogo_item", sa.Column("precio_puntos", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("catalogo_item", "precio_puntos")
