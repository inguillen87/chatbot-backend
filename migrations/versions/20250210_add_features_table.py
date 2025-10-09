"""add features table

Revision ID: 20250210_add_features_table
Revises: 20251008_add_encuestas_core
Create Date: 2025-02-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20250210_add_features_table"
down_revision = "20251008_add_encuestas_core"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "key", name="uq_features_owner_key"),
    )
    op.create_index("ix_features_owner_id", "features", ["owner_id"])
    op.create_index("ix_features_key", "features", ["key"])


def downgrade():
    op.drop_index("ix_features_key", table_name="features")
    op.drop_index("ix_features_owner_id", table_name="features")
    op.drop_table("features")
