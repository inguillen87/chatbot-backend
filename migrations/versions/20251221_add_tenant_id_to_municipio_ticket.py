"""Add tenant_id to municipio_ticket"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251221_add_tenant_id_to_municipio_ticket"
down_revision = "55c0c7b085ce"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("municipio_ticket", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_municipio_ticket_tenant_id_tenant_profile"),
            "tenant_profile",
            ["tenant_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_municipio_ticket_tenant_id"),
            ["tenant_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("municipio_ticket", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_municipio_ticket_tenant_id"))
        batch_op.drop_constraint(
            batch_op.f("fk_municipio_ticket_tenant_id_tenant_profile"),
            type_="foreignkey",
        )
        batch_op.drop_column("tenant_id")
