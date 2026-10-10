"""add idempotent territorial geocoding queue sync receipts

Revision ID: 20260830_geo_sync_v1
Revises: 20260830_geo_review_v1
Create Date: 2026-08-30 23:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_geo_sync_v1"
down_revision = "20260830_geo_review_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "territorial_geocoding_sync_receipt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64 AND length(request_digest) = 64",
            name="ck_territorial_geocoding_sync_digests",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_territorial_geocoding_sync_idempotency",
        ),
    )
    op.create_index(
        "ix_territorial_geocoding_sync_receipt_tenant_id",
        "territorial_geocoding_sync_receipt",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_sync_receipt_actor_user_id",
        "territorial_geocoding_sync_receipt",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_sync_tenant_created",
        "territorial_geocoding_sync_receipt",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_territorial_geocoding_sync_tenant_created",
        table_name="territorial_geocoding_sync_receipt",
    )
    op.drop_index(
        "ix_territorial_geocoding_sync_receipt_actor_user_id",
        table_name="territorial_geocoding_sync_receipt",
    )
    op.drop_index(
        "ix_territorial_geocoding_sync_receipt_tenant_id",
        table_name="territorial_geocoding_sync_receipt",
    )
    op.drop_table("territorial_geocoding_sync_receipt")
