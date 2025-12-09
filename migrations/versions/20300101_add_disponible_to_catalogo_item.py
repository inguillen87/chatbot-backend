"""Add catalogo_item availability and pricing

Revision ID: 20300101_add_disponible_to_catalogo_item
Revises: 20280416_merge_live_voting_and_whatsapp_heads
Create Date: 2030-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300101_add_disponible_to_catalogo_item'
down_revision = '20280416_merge_live_voting_and_whatsapp_heads'
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    cols = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- catalogo_item extras ---
    if not _has_column(inspector, "catalogo_item", "moneda"):
        op.add_column(
            "catalogo_item",
            sa.Column("moneda", sa.String(length=10), nullable=True),
        )

    if not _has_column(inspector, "catalogo_item", "precio_monetario"):
        op.add_column(
            "catalogo_item",
            sa.Column("precio_monetario", sa.Numeric(10, 2), nullable=True),
        )

    if not _has_column(inspector, "catalogo_item", "precio_por_caja"):
        op.add_column(
            "catalogo_item",
            sa.Column("precio_por_caja", sa.Numeric(10, 2), nullable=True),
        )

    if not _has_column(inspector, "catalogo_item", "unidad_por_caja"):
        op.add_column(
            "catalogo_item",
            sa.Column("unidad_por_caja", sa.Integer(), nullable=True),
        )

    if not _has_column(inspector, "catalogo_item", "pdf_url"):
        op.add_column(
            "catalogo_item",
            sa.Column("pdf_url", sa.String(length=512), nullable=True),
        )

    if not _has_column(inspector, "catalogo_item", "disponible"):
        # 1) agregarla nullable
        op.add_column(
            "catalogo_item",
            sa.Column("disponible", sa.Boolean(), nullable=True),
        )
        # 2) setear TRUE para todo lo existente
        op.execute(
            "UPDATE catalogo_item SET disponible = TRUE WHERE disponible IS NULL"
        )

        # 3) endurecer solo en engines que bancan ALTER COLUMN fácil
        if bind.dialect.name != "sqlite":
            op.alter_column(
                "catalogo_item",
                "disponible",
                nullable=False,
                server_default=sa.text("TRUE"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for col in [
        "disponible",
        "pdf_url",
        "unidad_por_caja",
        "precio_por_caja",
        "precio_monetario",
        "moneda",
    ]:
        if _has_column(inspector, "catalogo_item", col):
            op.drop_column("catalogo_item", col)
