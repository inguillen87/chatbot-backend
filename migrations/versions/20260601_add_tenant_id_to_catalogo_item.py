"""Add tenant_id column to catalogo_item

Revision ID: 20260601_add_tenant_id_to_catalogo_item
Revises: 20260406_merge_webauthn_public_survey_heads
Create Date: 2026-06-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260601_add_tenant_id_to_catalogo_item"
down_revision = "20260406_merge_webauthn_public_survey_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalogo_item",
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_catalogo_item_tenant_id_tenant_profile"),
        "catalogo_item",
        "tenant_profile",
        ["tenant_id"],
        ["id"],
    )
    op.create_index(op.f("ix_catalogo_item_tenant_id"), "catalogo_item", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_catalogo_item_tenant_id"), table_name="catalogo_item")
    op.drop_constraint(
        op.f("fk_catalogo_item_tenant_id_tenant_profile"), "catalogo_item", type_="foreignkey"
    )
    op.drop_column("catalogo_item", "tenant_id")
