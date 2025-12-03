"""add categoria_id to municipio_ticket

Revision ID: 55c0c7b085ce
Revises: 20291215_add_user_tenant_id_column
Create Date: 2025-12-03 21:17:01.525753

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '55c0c7b085ce'
down_revision = '20291215_add_user_tenant_id_column'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("municipio_ticket", schema=None) as batch_op:
        batch_op.add_column(sa.Column("categoria_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_municipio_ticket_categoria_id_categorias_ticket"),
            "categorias_ticket",
            ["categoria_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("municipio_ticket", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_municipio_ticket_categoria_id_categorias_ticket"), type_="foreignkey"
        )
        batch_op.drop_column("categoria_id")
