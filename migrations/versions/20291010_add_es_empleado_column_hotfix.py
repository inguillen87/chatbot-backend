"""Hotfix to add es_empleado column if missing

Revision ID: 20291010_add_es_empleado_column_hotfix
Revises: 20271201_add_es_empleado_if_missing
Create Date: 2029-10-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20291010_add_es_empleado_column_hotfix"
down_revision = "20271201_add_es_empleado_if_missing"
branch_labels = None
depends_on = None


def _user_columns(bind):
    inspector = sa.inspect(bind)
    if not inspector.has_table("user"):
        return set()
    return {col["name"] for col in inspector.get_columns("user")}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _user_columns(bind)

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
    columns = _user_columns(bind)

    if "es_empleado" in columns:
        op.drop_column("user", "es_empleado")
