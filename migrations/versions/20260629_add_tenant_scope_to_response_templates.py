"""add tenant scope to response templates

Revision ID: 20260629_response_templates_tenant
Revises: 20260521_single_tenant_owner
Create Date: 2026-06-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260629_response_templates_tenant"
down_revision = "20260521_single_tenant_owner"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("plantillas_respuesta") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_plantillas_respuesta_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_plantillas_respuesta_tenant_id_tenant_profile",
            "tenant_profile",
            ["tenant_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("plantillas_respuesta") as batch_op:
        batch_op.drop_constraint(
            "fk_plantillas_respuesta_tenant_id_tenant_profile",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_plantillas_respuesta_tenant_id")
        batch_op.drop_column("tenant_id")
