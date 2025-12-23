"""Add whatsapp_sender column to tenant_profile.

Revision ID: 20300110_add_whatsapp_sender_to_tenant_profile
Revises: 20300107_make_whatsapp_sender_id_unique
Create Date: 2030-01-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20300110_add_whatsapp_sender_to_tenant_profile"
down_revision = "20300107_make_whatsapp_sender_id_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}

    if "whatsapp_sender" not in columns:
        op.add_column(
            "tenant_profile",
            sa.Column("whatsapp_sender", sa.String(length=255), nullable=True),
        )
        op.create_unique_constraint(
            "uq_tenant_profile_whatsapp_sender", "tenant_profile", ["whatsapp_sender"]
        )
        op.create_index(
            op.f("ix_tenant_profile_whatsapp_sender"),
            "tenant_profile",
            ["whatsapp_sender"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    indexes = {index["name"] for index in inspector.get_indexes("tenant_profile")}
    unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("tenant_profile")
    }
    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}

    if op.f("ix_tenant_profile_whatsapp_sender") in indexes:
        op.drop_index(op.f("ix_tenant_profile_whatsapp_sender"), table_name="tenant_profile")

    if "uq_tenant_profile_whatsapp_sender" in unique_constraints:
        op.drop_constraint("uq_tenant_profile_whatsapp_sender", "tenant_profile", type_="unique")

    if "whatsapp_sender" in columns:
        op.drop_column("tenant_profile", "whatsapp_sender")
