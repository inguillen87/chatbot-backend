"""add public consulta pin to pyme tickets

Revision ID: 20260630_pyme_ticket_pin
Revises: 20260629_response_templates_tenant
Create Date: 2026-06-30 00:00:00.000000

"""
import random

from alembic import op
import sqlalchemy as sa


revision = "20260630_pyme_ticket_pin"
down_revision = "20260629_response_templates_tenant"
branch_labels = None
depends_on = None


def _backfill_pin_values() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE pyme_ticket
            SET consulta_pin = LPAD(FLOOR(RANDOM() * 900000 + 100000)::TEXT, 6, '0')
            WHERE consulta_pin IS NULL
            """
        )
        return

    rows = bind.execute(
        sa.text("SELECT id FROM pyme_ticket WHERE consulta_pin IS NULL")
    ).fetchall()
    for row in rows:
        bind.execute(
            sa.text("UPDATE pyme_ticket SET consulta_pin = :pin WHERE id = :id"),
            {"pin": f"{random.randint(100000, 999999)}", "id": row[0]},
        )


def upgrade():
    with op.batch_alter_table("pyme_ticket") as batch_op:
        batch_op.add_column(sa.Column("consulta_pin", sa.String(length=6), nullable=True))

    _backfill_pin_values()

    with op.batch_alter_table("pyme_ticket") as batch_op:
        batch_op.alter_column("consulta_pin", existing_type=sa.String(length=6), nullable=False)


def downgrade():
    with op.batch_alter_table("pyme_ticket") as batch_op:
        batch_op.drop_column("consulta_pin")
