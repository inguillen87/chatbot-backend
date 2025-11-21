"""Add assignment fields to tickets

Revision ID: 20260630_add_ticket_assignment_fields
Revises: 20260622_add_metadata_to_catalogo_item
Create Date: 2026-06-30 00:00:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260630_add_ticket_assignment_fields"
down_revision = "20260622_add_metadata_to_catalogo_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "municipio_ticket",
        sa.Column("asignado_a_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "municipio_ticket",
        sa.Column("asignado_en", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_municipio_ticket_asignado_a_id"),
        "municipio_ticket",
        ["asignado_a_id"],
        unique=False,
    )

    op.add_column(
        "pyme_ticket",
        sa.Column("asignado_a_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "pyme_ticket",
        sa.Column("asignado_en", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_pyme_ticket_asignado_a_id"),
        "pyme_ticket",
        ["asignado_a_id"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_municipio_ticket_asignado_a_user",
        "municipio_ticket",
        "user",
        ["asignado_a_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_pyme_ticket_asignado_a_user",
        "pyme_ticket",
        "user",
        ["asignado_a_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_pyme_ticket_asignado_a_user", "pyme_ticket", type_="foreignkey")
    op.drop_index(op.f("ix_pyme_ticket_asignado_a_id"), table_name="pyme_ticket")
    op.drop_column("pyme_ticket", "asignado_en")
    op.drop_column("pyme_ticket", "asignado_a_id")

    op.drop_constraint("fk_municipio_ticket_asignado_a_user", "municipio_ticket", type_="foreignkey")
    op.drop_index(op.f("ix_municipio_ticket_asignado_a_id"), table_name="municipio_ticket")
    op.drop_column("municipio_ticket", "asignado_en")
    op.drop_column("municipio_ticket", "asignado_a_id")
