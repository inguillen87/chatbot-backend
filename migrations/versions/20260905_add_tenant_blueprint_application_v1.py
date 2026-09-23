"""add tenant blueprint application receipts v1

Revision ID: 20260905_tenant_blueprint_v1
Revises: 20260904_geo_execution_v2
Create Date: 2026-09-05 12:00:00.000000
"""

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision = "20260905_tenant_blueprint_v1"
down_revision = "20260904_geo_execution_v2"
branch_labels = None
depends_on = None


_JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "tenant_blueprint_application",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "contract_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("blueprint_id", sa.String(length=64), nullable=False),
        sa.Column("blueprint_version", sa.String(length=32), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="applied",
        ),
        sa.Column("application_snapshot", _JSON_TYPE, nullable=False),
        sa.Column("applied_by_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status = 'applied'",
            name="ck_tenant_blueprint_application_status",
        ),
        sa.CheckConstraint(
            "length(manifest_digest) = 64 AND "
            "length(request_digest) = 64 AND "
            "length(idempotency_key_hash) = 64",
            name="ck_tenant_blueprint_application_digests",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["applied_by_user_id"],
            ["user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "blueprint_id",
            "blueprint_version",
            name="uq_tenant_blueprint_application_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_tenant_blueprint_application_idempotency",
        ),
    )
    op.create_index(
        "ix_tenant_blueprint_application_tenant_id",
        "tenant_blueprint_application",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_blueprint_application_applied_by_user_id",
        "tenant_blueprint_application",
        ["applied_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_blueprint_application_tenant_created",
        "tenant_blueprint_application",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_blueprint_application_tenant_created",
        table_name="tenant_blueprint_application",
    )
    op.drop_index(
        "ix_tenant_blueprint_application_applied_by_user_id",
        table_name="tenant_blueprint_application",
    )
    op.drop_index(
        "ix_tenant_blueprint_application_tenant_id",
        table_name="tenant_blueprint_application",
    )
    op.drop_table("tenant_blueprint_application")
