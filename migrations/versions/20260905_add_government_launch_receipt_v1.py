"""add append-only government launch receipts v1

Revision ID: 20260905_government_launch_v1
Revises: 20260905_tenant_blueprint_v1
Create Date: 2026-09-05 16:00:00.000000
"""

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision = "20260905_government_launch_v1"
down_revision = "20260905_tenant_blueprint_v1"
branch_labels = None
depends_on = None


_JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION tenant_blueprint_launch_receipt_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'tenant blueprint launch receipts are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tenant_blueprint_launch_receipt_immutable
            BEFORE UPDATE OR DELETE ON tenant_blueprint_launch_receipt
            FOR EACH ROW EXECUTE FUNCTION tenant_blueprint_launch_receipt_immutable()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_tenant_blueprint_launch_receipt_update
            BEFORE UPDATE ON tenant_blueprint_launch_receipt
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'tenant blueprint launch receipts are append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tenant_blueprint_launch_receipt_delete
            BEFORE DELETE ON tenant_blueprint_launch_receipt
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'tenant blueprint launch receipts are append-only');
            END
            """
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_tenant_blueprint_launch_receipt_immutable "
            "ON tenant_blueprint_launch_receipt"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS tenant_blueprint_launch_receipt_immutable()"
        )
    elif dialect == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_tenant_blueprint_launch_receipt_delete"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_tenant_blueprint_launch_receipt_update"
        )


def upgrade() -> None:
    op.create_table(
        "tenant_blueprint_launch_receipt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("blueprint_application_id", sa.String(length=36), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("blueprint_id", sa.String(length=64), nullable=False),
        sa.Column("blueprint_version", sa.String(length=32), nullable=False),
        sa.Column("launch_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("launch_digest", sa.String(length=64), nullable=False),
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
            name="ck_tenant_blueprint_launch_status",
        ),
        sa.CheckConstraint(
            "length(manifest_digest) = 64 AND "
            "length(launch_digest) = 64 AND "
            "length(request_digest) = 64 AND "
            "length(idempotency_key_hash) = 64",
            name="ck_tenant_blueprint_launch_digests",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["blueprint_application_id"],
            ["tenant_blueprint_application.id"],
            ondelete="RESTRICT",
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
            "launch_id",
            name="uq_tenant_blueprint_launch_module_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_tenant_blueprint_launch_idempotency",
        ),
    )
    op.create_index(
        "ix_tenant_blueprint_launch_tenant_id",
        "tenant_blueprint_launch_receipt",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_blueprint_launch_blueprint_application_id",
        "tenant_blueprint_launch_receipt",
        ["blueprint_application_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_blueprint_launch_applied_by_user_id",
        "tenant_blueprint_launch_receipt",
        ["applied_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_blueprint_launch_tenant_created",
        "tenant_blueprint_launch_receipt",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )
    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_index(
        "ix_tenant_blueprint_launch_tenant_created",
        table_name="tenant_blueprint_launch_receipt",
    )
    op.drop_index(
        "ix_tenant_blueprint_launch_applied_by_user_id",
        table_name="tenant_blueprint_launch_receipt",
    )
    op.drop_index(
        "ix_tenant_blueprint_launch_blueprint_application_id",
        table_name="tenant_blueprint_launch_receipt",
    )
    op.drop_index(
        "ix_tenant_blueprint_launch_tenant_id",
        table_name="tenant_blueprint_launch_receipt",
    )
    op.drop_table("tenant_blueprint_launch_receipt")
