"""agrego los campos que faltan

Revision ID: 16040fa374be
Revises: 2cbd47100727
Create Date: 2025-08-07 00:28:16.800957
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "16040fa374be"
down_revision = "2cbd47100727"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Devuelve True si la columna existe en la tabla (DB actual)."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns(table_name)]
    return column_name in cols


def upgrade():
    # Si quedó una tabla temporal de un intento fallido, ignoramos si no existe
    try:
        op.drop_table("_alembic_tmp_pyme_ticket")
    except Exception:


def downgrade():
    # Add back seguro: solo si la columna NO existe
    if not _has_column("pyme_ticket", "archivo_url"):
        with op.batch_alter_table("pyme_ticket") as batch_op:
            batch_op.add_column(sa.Column("archivo_url", sa.String(255), nullable=True))
