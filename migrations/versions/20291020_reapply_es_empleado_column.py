"""Reapply es_empleado column to user table if missing

Revision ID: 20291020_reapply_es_empleado_column
Revises: 20291010_add_es_empleado_column_hotfix
Create Date: 2029-10-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20291020_reapply_es_empleado_column"
down_revision = "20291010_add_es_empleado_column_hotfix"
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
