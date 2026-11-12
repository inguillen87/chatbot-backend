"""add tenant-scoped territorial geocoding queue and audit attempts

Revision ID: 20260830_territorial_geocoding_v1
Revises: 20260829_global_writer_authority_v1
Create Date: 2026-08-30 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_territorial_geocoding_v1"
down_revision = "20260829_global_writer_authority_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "territorial_geocoding_job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("source_model", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("address_digest", sa.String(length=64), nullable=False),
        sa.Column("jurisdiction_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_place_id", sa.String(length=255), nullable=True),
        sa.Column("proposed_lat", sa.Float(), nullable=True),
        sa.Column("proposed_lng", sa.Float(), nullable=True),
        sa.Column("location_type", sa.String(length=32), nullable=True),
        sa.Column("partial_match", sa.Boolean(), nullable=True),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'needs_review', 'applied', 'failed')",
            name="ck_territorial_geocoding_job_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_territorial_geocoding_job_attempt_count",
        ),
        sa.CheckConstraint(
            "length(candidate_fingerprint) = 64",
            name="ck_territorial_geocoding_job_fingerprint",
        ),
        sa.CheckConstraint(
            "length(address_digest) = 64",
            name="ck_territorial_geocoding_job_address_digest",
        ),
        sa.CheckConstraint(
            "length(jurisdiction_digest) = 64",
            name="ck_territorial_geocoding_job_jurisdiction_digest",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_fingerprint",
            name="uq_territorial_geocoding_job_candidate",
        ),
    )
    op.create_index(
        "ix_territorial_geocoding_job_tenant_id",
        "territorial_geocoding_job",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_job_tenant_status_created",
        "territorial_geocoding_job",
        ["tenant_id", "status", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "territorial_geocoding_attempt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("outcome_status", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column(
            "external_call_performed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "write_performed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "outcome_status IN ('pending', 'needs_review', 'applied', 'failed')",
            name="ck_territorial_geocoding_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_territorial_geocoding_attempt_number",
        ),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name="ck_territorial_geocoding_attempt_request_digest",
        ),
        sa.CheckConstraint(
            "length(result_digest) = 64",
            name="ck_territorial_geocoding_attempt_result_digest",
        ),
        sa.CheckConstraint(
            "write_performed IS FALSE OR outcome_status = 'applied'",
            name="ck_territorial_geocoding_attempt_write_state",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["territorial_geocoding_job.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "request_digest",
            name="uq_territorial_geocoding_attempt_request",
        ),
        sa.UniqueConstraint(
            "job_id",
            "attempt_number",
            name="uq_territorial_geocoding_attempt_number",
        ),
    )
    op.create_index(
        "ix_territorial_geocoding_attempt_job_id",
        "territorial_geocoding_attempt",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_attempt_tenant_id",
        "territorial_geocoding_attempt",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_territorial_geocoding_attempt_tenant_created",
        "territorial_geocoding_attempt",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_territorial_geocoding_attempt_tenant_created",
        table_name="territorial_geocoding_attempt",
    )
    op.drop_index(
        "ix_territorial_geocoding_attempt_tenant_id",
        table_name="territorial_geocoding_attempt",
    )
    op.drop_index(
        "ix_territorial_geocoding_attempt_job_id",
        table_name="territorial_geocoding_attempt",
    )
    op.drop_table("territorial_geocoding_attempt")
    op.drop_index(
        "ix_territorial_geocoding_job_tenant_status_created",
        table_name="territorial_geocoding_job",
    )
    op.drop_index(
        "ix_territorial_geocoding_job_tenant_id",
        table_name="territorial_geocoding_job",
    )
    op.drop_table("territorial_geocoding_job")
