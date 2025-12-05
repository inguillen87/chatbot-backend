"""Add whatsapp_sender_id column to tenant_profile.

Revision ID: 20261205_add_whatsapp_sender_id_to_tenant_profile
Revises: 20261205_add_plan_to_tenant_profile
Create Date: 2026-12-05 00:00:00.000001
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20261205_add_whatsapp_sender_id_to_tenant_profile"
down_revision = "20261205_add_plan_to_tenant_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}

    if "whatsapp_sender_id" not in columns:
        op.add_column(
            "tenant_profile",
            sa.Column("whatsapp_sender_id", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}

    if "whatsapp_sender_id" in columns:
        op.drop_column("tenant_profile", "whatsapp_sender_id")
