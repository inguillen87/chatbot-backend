"""add secure tenant claim intake receipts

Revision ID: 20260728_claim_receipts
Revises: 20260728_survey_logic
Create Date: 2026-07-28 00:00:02.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_claim_receipts"
down_revision = "20260728_survey_logic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenant_ticket", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("intake_idempotency_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("intake_payload_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("claim_receipt_secret_version", sa.String(length=16), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_tenant_ticket_tenant_intake_idempotency",
            ["tenant_id", "intake_idempotency_hash"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tenant_ticket", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_tenant_ticket_tenant_intake_idempotency",
            type_="unique",
        )
        batch_op.drop_column("claim_receipt_secret_version")
        batch_op.drop_column("intake_payload_hash")
        batch_op.drop_column("intake_idempotency_hash")
