"""harden territorial geocoding execution fencing

Revision ID: 20260904_geo_execution_v2
Revises: 20260831_inbox_artifact_v1
Create Date: 2026-09-04 22:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_geo_execution_v2"
down_revision = "20260831_inbox_artifact_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("territorial_geocoding_attempt") as batch_op:
        batch_op.add_column(sa.Column("action", sa.String(length=16), nullable=True))
        batch_op.add_column(
            sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_territorial_geocoding_attempt_idempotency",
            "(action IS NULL AND idempotency_key_hash IS NULL) OR "
            "(action IN ('resolve', 'apply') AND length(idempotency_key_hash) = 64)",
        )
        batch_op.create_unique_constraint(
            "uq_territorial_geocoding_attempt_idempotency",
            ["job_id", "action", "idempotency_key_hash"],
        )

    with op.batch_alter_table("territorial_geocoding_review") as batch_op:
        # Existing approvals deliberately remain unbound and therefore stale.
        # Only reviews created after this migration can authorize a write.
        batch_op.add_column(
            sa.Column("proposal_attempt_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("proposal_attempt_number", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_territorial_geocoding_review_proposal_attempt",
            "territorial_geocoding_attempt",
            ["proposal_attempt_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_territorial_geocoding_review_proposal_attempt",
            "(proposal_attempt_id IS NULL AND proposal_attempt_number IS NULL) OR "
            "(length(proposal_attempt_id) = 36 AND proposal_attempt_number > 0)",
        )
        batch_op.create_index(
            "ix_territorial_geocoding_review_proposal_attempt_id",
            ["proposal_attempt_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("territorial_geocoding_review") as batch_op:
        batch_op.drop_index("ix_territorial_geocoding_review_proposal_attempt_id")
        batch_op.drop_constraint(
            "ck_territorial_geocoding_review_proposal_attempt", type_="check"
        )
        batch_op.drop_constraint(
            "fk_territorial_geocoding_review_proposal_attempt", type_="foreignkey"
        )
        batch_op.drop_column("proposal_attempt_number")
        batch_op.drop_column("proposal_attempt_id")

    with op.batch_alter_table("territorial_geocoding_attempt") as batch_op:
        batch_op.drop_constraint(
            "uq_territorial_geocoding_attempt_idempotency", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_territorial_geocoding_attempt_idempotency", type_="check"
        )
        batch_op.drop_column("idempotency_key_hash")
        batch_op.drop_column("action")
