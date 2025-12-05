"""Add plan column to tenant_profile.

Revision ID: 20261205_add_plan_to_tenant_profile
Revises: 20251223_add_missing_tenant_id_municipio_ticket
Create Date: 2026-12-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20261205_add_plan_to_tenant_profile"
down_revision = "20251223_add_missing_tenant_id_municipio_ticket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}

    if "plan" not in columns:
        op.add_column(
            "tenant_profile",
            sa.Column("plan", sa.String(length=50), server_default="free", nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}

    if "plan" in columns:
        op.drop_column("tenant_profile", "plan")
