"""Add es_empleado flag to user

Revision ID: 20270221_add_es_empleado_to_user
Revises: 20251130_add_puntos_recompensa_to_enc_encuesta
Create Date: 2027-02-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20270221_add_es_empleado_to_user"
down_revision = "20251130_add_puntos_recompensa_to_enc_encuesta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "es_empleado",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.alter_column("user", "es_empleado", server_default=None)


def downgrade() -> None:
    op.drop_column("user", "es_empleado")
