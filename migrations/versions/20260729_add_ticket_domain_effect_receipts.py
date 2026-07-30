"""add durable ticket domain effect receipts

Revision ID: 20260729_ticket_effects
Revises: 20260729_whatsapp_turns
Create Date: 2026-07-29 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260729_ticket_effects"
down_revision = "20260729_whatsapp_turns"
branch_labels = None
depends_on = None


def _json_type():
    bind = op.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    op.create_table(
        "ticket_domain_effect_receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=191), nullable=False),
        sa.Column("effect_kind", sa.String(length=48), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("result_json", _json_type(), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="ticket.domain_effect.v1",
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
            "effect_kind IN ('ticket.create.municipio', 'ticket.create.pyme', "
            "'ticket.comment.municipio', 'ticket.comment.pyme')",
            name="ck_ticket_domain_effect_kind",
        ),
        sa.CheckConstraint(
            "resource_type IN ('municipio_ticket', 'pyme_ticket', 'ticket_comentario')",
            name="ck_ticket_domain_effect_resource_type",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_ticket_domain_effect_payload_hash",
        ),
        sa.CheckConstraint(
            "resource_id > 0",
            name="ck_ticket_domain_effect_resource_id_positive",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_ticket_domain_effect_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_ticket_domain_effect_tenant_kind",
        "ticket_domain_effect_receipt",
        ["tenant_id", "effect_kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ticket_domain_effect_resource",
        "ticket_domain_effect_receipt",
        ["resource_type", "resource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ticket_domain_effect_resource",
        table_name="ticket_domain_effect_receipt",
    )
    op.drop_index(
        "ix_ticket_domain_effect_tenant_kind",
        table_name="ticket_domain_effect_receipt",
    )
    op.drop_table("ticket_domain_effect_receipt")
