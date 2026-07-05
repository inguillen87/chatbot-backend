"""add legacy ticket metadata json columns

Revision ID: 20260705_ticket_ai_metadata
Revises: 20260630_pyme_ticket_pin
Create Date: 2026-07-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260705_ticket_ai_metadata"
down_revision = "20260630_pyme_ticket_pin"
branch_labels = None
depends_on = None


def _table_columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    json_type = sa.JSON()

    if "datos_extra" not in _table_columns("municipio_ticket"):
        with op.batch_alter_table("municipio_ticket") as batch_op:
            batch_op.add_column(sa.Column("datos_extra", json_type, nullable=True))

    if "datos_extra" not in _table_columns("pyme_ticket"):
        with op.batch_alter_table("pyme_ticket") as batch_op:
            batch_op.add_column(sa.Column("datos_extra", json_type, nullable=True))


def downgrade() -> None:
    if "datos_extra" in _table_columns("pyme_ticket"):
        with op.batch_alter_table("pyme_ticket") as batch_op:
            batch_op.drop_column("datos_extra")

    if "datos_extra" in _table_columns("municipio_ticket"):
        with op.batch_alter_table("municipio_ticket") as batch_op:
            batch_op.drop_column("datos_extra")
