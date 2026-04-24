"""Ensure es_empleado column exists on user table

Revision ID: 20271201_add_es_empleado_if_missing
Revises: 20270221_add_es_empleado_to_user
Create Date: 2027-12-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20271201_add_es_empleado_if_missing"
down_revision = "20270221_add_es_empleado_to_user"
branch_labels = None
depends_on = None


def _get_user_columns(bind):
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns("user")}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _get_user_columns(bind)

    if "es_empleado" not in columns:
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
    bind = op.get_bind()
    columns = _get_user_columns(bind)

    if "es_empleado" in columns:
        op.drop_column("user", "es_empleado")
