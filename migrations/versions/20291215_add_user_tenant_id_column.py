"""Add tenant_id column to user table if missing

Revision ID: 20291215_add_user_tenant_id_column
Revises: 20291020_reapply_es_empleado_column
Create Date: 2029-12-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20291215_add_user_tenant_id_column"
down_revision = "20291020_reapply_es_empleado_column"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = [column["name"] for column in inspector.get_columns("user")]

    if "tenant_id" not in columns:
        op.add_column("user", sa.Column("tenant_id", sa.Integer(), nullable=True))

        tables = inspector.get_table_names()
        if "tenant" in tables:
            op.create_foreign_key(
                "fk_user_tenant_id_tenant",
                "user",
                "tenant",
                ["tenant_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = [column["name"] for column in inspector.get_columns("user")]

    if "tenant_id" in columns:
        fk_names = {fk["name"] for fk in inspector.get_foreign_keys("user") if fk.get("name")}

        if "fk_user_tenant_id_tenant" in fk_names:
            op.drop_constraint(
                "fk_user_tenant_id_tenant",
                "user",
                type_="foreignkey",
            )

        op.drop_column("user", "tenant_id")
