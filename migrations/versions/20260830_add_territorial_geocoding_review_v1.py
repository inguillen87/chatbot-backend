"""add immutable tenant-scoped territorial geocoding reviews

Revision ID: 20260830_geo_review_v1
Revises: 20260830_territorial_geocoding_v1
Create Date: 2026-08-30 21:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_geo_review_v1"
down_revision = "20260830_territorial_geocoding_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "territorial_geocoding_review",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_user_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("reviewed_job_status", sa.String(length=20), nullable=False),
        sa.Column("proposal_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "coordinate_write_performed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_territorial_geocoding_review_decision",
        ),
        sa.CheckConstraint(
            "reviewed_job_status IN ('pending', 'needs_review', 'failed')",
            name="ck_territorial_geocoding_review_job_status",
        ),
        sa.CheckConstraint(
            "length(proposal_digest) = 64 AND "
            "length(idempotency_key_hash) = 64 AND "
            "length(request_digest) = 64",
            name="ck_territorial_geocoding_review_digests",
        ),
        sa.CheckConstraint(
            "coordinate_write_performed IS FALSE",
            name="ck_territorial_geocoding_review_no_coordinate_write",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["territorial_geocoding_job.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "job_id",
            "idempotency_key_hash",
            name="uq_territorial_geocoding_review_idempotency",
        ),
    )
    op.create_index(
        "ix_territorial_geocoding_review_job_id",
        "territorial_geocoding_review",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_review_tenant_id",
        "territorial_geocoding_review",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_review_reviewer_user_id",
        "territorial_geocoding_review",
        ["reviewer_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_review_tenant_job_created",
        "territorial_geocoding_review",
        ["tenant_id", "job_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_territorial_geocoding_review_tenant_job_created",
        table_name="territorial_geocoding_review",
    )
    op.drop_index(
        "ix_territorial_geocoding_review_reviewer_user_id",
        table_name="territorial_geocoding_review",
    )
    op.drop_index(
        "ix_territorial_geocoding_review_tenant_id",
        table_name="territorial_geocoding_review",
    )
    op.drop_index(
        "ix_territorial_geocoding_review_job_id",
        table_name="territorial_geocoding_review",
    )
    op.drop_table("territorial_geocoding_review")
