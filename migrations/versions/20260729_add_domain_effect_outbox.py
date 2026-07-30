"""add generic durable domain effect outbox

Revision ID: 20260729_domain_effect_outbox
Revises: 20260729_pyme_order_payload_hash
Create Date: 2026-07-29 23:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260729_domain_effect_outbox"
down_revision = "20260729_pyme_order_payload_hash"
branch_labels = None
depends_on = None


def _json_type():
    bind = op.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _lower_hex64_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column_name}) = 64 AND length({remainder}) = 0"


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "domain_effect_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=48), nullable=False),
        sa.Column("aggregate_ref", sa.String(length=191), nullable=False),
        sa.Column("effect_type", sa.String(length=96), nullable=False),
        sa.Column("handler_name", sa.String(length=96), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("recipient_ref", sa.String(length=191), nullable=False),
        sa.Column("effect_key", sa.String(length=191), nullable=False),
        sa.Column("intent_hmac", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="8",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("io_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_ref_hash", sa.String(length=64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", json_type, nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="domain.effect_outbox.v1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(aggregate_type) BETWEEN 1 AND 48 "
            "AND trim(aggregate_type) = aggregate_type",
            name="ck_domain_effect_aggregate_type",
        ),
        sa.CheckConstraint(
            "length(aggregate_ref) BETWEEN 1 AND 191 "
            "AND trim(aggregate_ref) = aggregate_ref",
            name="ck_domain_effect_aggregate_ref",
        ),
        sa.CheckConstraint(
            "length(effect_type) BETWEEN 1 AND 96 AND trim(effect_type) = effect_type",
            name="ck_domain_effect_effect_type",
        ),
        sa.CheckConstraint(
            "length(handler_name) BETWEEN 1 AND 96 AND trim(handler_name) = handler_name",
            name="ck_domain_effect_handler_name",
        ),
        sa.CheckConstraint(
            "channel IN ('sigem', 'email', 'sms', 'whatsapp', 'realtime', 'internal')",
            name="ck_domain_effect_channel",
        ),
        sa.CheckConstraint(
            "length(recipient_ref) BETWEEN 1 AND 191 "
            "AND trim(recipient_ref) = recipient_ref",
            name="ck_domain_effect_recipient_ref",
        ),
        sa.CheckConstraint(
            "length(effect_key) BETWEEN 1 AND 191 AND trim(effect_key) = effect_key",
            name="ck_domain_effect_effect_key",
        ),
        sa.CheckConstraint(
            _lower_hex64_check("intent_hmac"),
            name="ck_domain_effect_intent_hmac",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_wait', 'succeeded', "
            "'skipped', 'unknown', 'dead')",
            name="ck_domain_effect_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_domain_effect_attempts",
        ),
        sa.CheckConstraint(
            "lease_token IS NULL OR (length(lease_token) BETWEEN 1 AND 64 "
            "AND trim(lease_token) = lease_token)",
            name="ck_domain_effect_lease_token",
        ),
        sa.CheckConstraint(
            "((status = 'processing' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) "
            "OR (status <> 'processing' AND lease_token IS NULL AND leased_until IS NULL))",
            name="ck_domain_effect_lease_state",
        ),
        sa.CheckConstraint(
            "((status IN ('succeeded', 'skipped', 'unknown', 'dead') "
            "AND processed_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded', 'skipped', 'unknown', 'dead') "
            "AND processed_at IS NULL))",
            name="ck_domain_effect_terminal_time",
        ),
        sa.CheckConstraint(
            "io_started_at IS NULL OR status IN ('processing', 'succeeded', "
            "'skipped', 'unknown', 'dead')",
            name="ck_domain_effect_io_state",
        ),
        sa.CheckConstraint(
            "provider_ref_hash IS NULL OR ("
            + _lower_hex64_check("provider_ref_hash")
            + ")",
            name="ck_domain_effect_provider_ref_hash",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64 "
            "AND trim(last_error_code) = last_error_code)",
            name="ck_domain_effect_error_code",
        ),
        sa.CheckConstraint(
            "last_error_digest IS NULL OR ("
            + _lower_hex64_check("last_error_digest")
            + ")",
            name="ck_domain_effect_error_digest",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "effect_key",
            name="uq_domain_effect_tenant_key",
        ),
    )
    op.create_index(
        "ix_domain_effect_due",
        "domain_effect_outbox",
        ["status", "available_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_domain_effect_stale",
        "domain_effect_outbox",
        ["status", "leased_until", "id"],
        unique=False,
    )
    op.create_index(
        "ix_domain_effect_aggregate",
        "domain_effect_outbox",
        ["tenant_id", "aggregate_type", "aggregate_ref", "id"],
        unique=False,
    )
    op.create_index(
        "ix_domain_effect_tenant_status",
        "domain_effect_outbox",
        ["tenant_id", "status", "updated_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_domain_effect_tenant_status",
        table_name="domain_effect_outbox",
    )
    op.drop_index(
        "ix_domain_effect_aggregate",
        table_name="domain_effect_outbox",
    )
    op.drop_index(
        "ix_domain_effect_stale",
        table_name="domain_effect_outbox",
    )
    op.drop_index(
        "ix_domain_effect_due",
        table_name="domain_effect_outbox",
    )
    op.drop_table("domain_effect_outbox")
