"""add entity_token and tenant_slug to user

Revision ID: 20260918_add_entity_token_and_tenant_slug
Revises: 20260705_add_email_verification_fields
Create Date: 2026-09-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260918_add_entity_token_and_tenant_slug"
down_revision = "20260705_add_email_verification_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user",
        sa.Column("entity_token", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("tenant_slug", sa.String(length=150), nullable=True),
    )
    op.create_index("ix_user_entity_token", "user", ["entity_token"], unique=True)
    op.create_index("ix_user_tenant_slug", "user", ["tenant_slug"], unique=False)

    user_table = sa.table(
        "user",
        sa.column("id", sa.Integer),
        sa.column("entity_token", sa.String),
        sa.column("token", sa.String),
    )

    conn = op.get_bind()
    conn.execute(  # type: ignore[arg-type]
        user_table.update()
        .where(sa.and_(user_table.c.entity_token == None, user_table.c.token != None))
        .values(entity_token=user_table.c.token)
    )


def downgrade():
    op.drop_index("ix_user_tenant_slug", table_name="user")
    op.drop_index("ix_user_entity_token", table_name="user")
    op.drop_column("user", "tenant_slug")
    op.drop_column("user", "entity_token")
