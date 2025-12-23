"""Add whatsapp_sender to tenant_profile.

Revision ID: 20300110_add_whatsapp_sender_to_tenant_profile
Revises: 20300108_add_tenant_is_active
Create Date: 2030-01-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20300110_add_whatsapp_sender_to_tenant_profile"
# Chain directly after the is_active migration to avoid multiple heads.
down_revision = "20300108_add_tenant_is_active"
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

    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("tenant_profile")
    }
    indexes = {index["name"] for index in inspector.get_indexes("tenant_profile")}

    with op.batch_alter_table("tenant_profile") as batch_op:
        if "uq_tenant_profile_whatsapp_sender" not in unique_constraints:
            batch_op.create_unique_constraint(
                "uq_tenant_profile_whatsapp_sender",
                ["whatsapp_sender"],
            )
        if "ix_tenant_profile_whatsapp_sender" not in indexes:
            batch_op.create_index(
                "ix_tenant_profile_whatsapp_sender",
                ["whatsapp_sender"],
                unique=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tenant_profile")}
    unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("tenant_profile")
    }
    indexes = {index["name"] for index in inspector.get_indexes("tenant_profile")}

    with op.batch_alter_table("tenant_profile") as batch_op:
        if "ix_tenant_profile_whatsapp_sender" in indexes:
            batch_op.drop_index("ix_tenant_profile_whatsapp_sender")
        if "uq_tenant_profile_whatsapp_sender" in unique_constraints:
            batch_op.drop_constraint(
                "uq_tenant_profile_whatsapp_sender",
                type_="unique",
            )

    if "whatsapp_sender" in columns:
        op.drop_column("tenant_profile", "whatsapp_sender")
