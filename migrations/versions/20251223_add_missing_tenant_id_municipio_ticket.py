"""Ensure tenant_id exists on municipio_ticket

Revision ID: 20251223_add_missing_tenant_id_municipio_ticket
Revises: 20251221_add_tenant_id_to_municipio_ticket
Create Date: 2025-12-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20251223_add_missing_tenant_id_municipio_ticket"
down_revision = "20251221_add_tenant_id_to_municipio_ticket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("municipio_ticket")}
    indexes = {index["name"] for index in inspector.get_indexes("municipio_ticket")}
    fk_names = {fk["name"] for fk in inspector.get_foreign_keys("municipio_ticket") if fk.get("name")}
    tables = set(inspector.get_table_names())

    if "tenant_id" not in columns:
        op.add_column("municipio_ticket", sa.Column("tenant_id", sa.Integer(), nullable=True))
        columns.add("tenant_id")

    if "tenant_id" in columns:
        if "ix_municipio_ticket_tenant_id" not in indexes:
            op.create_index(op.f("ix_municipio_ticket_tenant_id"), "municipio_ticket", ["tenant_id"], unique=False)

        if "fk_municipio_ticket_tenant_id_tenant_profile" not in fk_names and "tenant_profile" in tables:
            op.create_foreign_key(
                op.f("fk_municipio_ticket_tenant_id_tenant_profile"),
                "municipio_ticket",
                "tenant_profile",
                ["tenant_id"],
                ["id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("municipio_ticket")}
    indexes = {index["name"] for index in inspector.get_indexes("municipio_ticket")}
    fk_names = {fk["name"] for fk in inspector.get_foreign_keys("municipio_ticket") if fk.get("name")}

    if "ix_municipio_ticket_tenant_id" in indexes:
        op.drop_index(op.f("ix_municipio_ticket_tenant_id"), table_name="municipio_ticket")

    if "fk_municipio_ticket_tenant_id_tenant_profile" in fk_names:
        op.drop_constraint(op.f("fk_municipio_ticket_tenant_id_tenant_profile"), "municipio_ticket", type_="foreignkey")

    if "tenant_id" in columns:
        op.drop_column("municipio_ticket", "tenant_id")
