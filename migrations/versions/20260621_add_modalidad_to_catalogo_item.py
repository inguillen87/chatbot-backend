"""Add modalidad column to catalogo_item

Revision ID: 20260621_add_modalidad_to_catalogo_item
Revises: 20260620_add_precio_puntos_to_catalogo_item
Create Date: 2026-06-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260621_add_modalidad_to_catalogo_item"
down_revision = "20260620_add_precio_puntos_to_catalogo_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalogo_item",
        sa.Column(
            "modalidad",
            sa.String(length=20),
            nullable=False,
            server_default="venta",
        ),
    )
    # Optional: drop the server default after backfilling existing rows
    op.alter_column(
        "catalogo_item",
        "modalidad",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("catalogo_item", "modalidad")
