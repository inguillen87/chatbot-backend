"""add durable municipal chat idempotency receipts

Revision ID: 20260825_chat_idempotency_v1
Revises: 20260825_legacy_municipio_ticket_scope_repair_v1
Create Date: 2026-08-25 18:00:00.000000

Only digests of the client key, actor/session scope and canonical request are
stored.  A completed JSON response snapshot is retained for exact replay, then
can be scrubbed into a non-reexecuting tombstone by the retention policy.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260825_chat_idempotency_v1"
down_revision = "20260825_legacy_municipio_ticket_scope_repair_v1"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "municipio_chat_idempotency_receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=80), nullable=False),
        sa.Column("actor_scope_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="processing",
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_json", JSON_TYPE, nullable=True),
        sa.Column("response_request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="chat.municipio.idempotency.v1",
        ),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'expired')",
            name="ck_municipio_chat_idempotency_status",
        ),
        sa.CheckConstraint(
            "length(actor_scope_hash) = 64 AND "
            "length(idempotency_key_hash) = 64 AND "
            "length(request_hash) = 64",
            name="ck_municipio_chat_idempotency_hashes",
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND response_status IS NULL "
            "AND response_json IS NULL AND completed_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'completed' AND response_status BETWEEN 100 AND 599 "
            "AND response_json IS NOT NULL AND completed_at IS NOT NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'expired' AND response_status IS NULL "
            "AND response_json IS NULL AND response_request_id IS NULL "
            "AND completed_at IS NOT NULL AND expired_at IS NOT NULL)",
            name="ck_municipio_chat_idempotency_completion",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "endpoint",
            "actor_scope_hash",
            "idempotency_key_hash",
            name="uq_municipio_chat_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_municipio_chat_idempotency_tenant_created",
        "municipio_chat_idempotency_receipt",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_municipio_chat_idempotency_tenant_created",
        table_name="municipio_chat_idempotency_receipt",
    )
    op.drop_table("municipio_chat_idempotency_receipt")
