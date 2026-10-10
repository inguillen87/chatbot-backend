"""add persistent global writer authority

Revision ID: 20260829_global_writer_authority_v1
Revises: 20260829_inbound_fifo_v2
Create Date: 2026-08-29 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260829_global_writer_authority_v1"
down_revision = "20260829_inbound_fifo_v2"
branch_labels = None
depends_on = None


TABLE_NAME = "cutover_global_writer_authority"
AUTHORITY_KEY = "primary"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("authority_key", sa.String(length=32), nullable=False),
        sa.Column("owner_runtime", sa.String(length=16), nullable=True),
        sa.Column(
            "epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "render_fenced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "vercel_fenced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "authority_key = 'primary'",
            name="ck_cutover_global_writer_authority_singleton",
        ),
        sa.CheckConstraint(
            "owner_runtime IS NULL OR owner_runtime IN ('render', 'vercel')",
            name="ck_cutover_global_writer_authority_owner",
        ),
        sa.CheckConstraint(
            "(owner_runtime IS NULL AND render_fenced IS TRUE "
            "AND vercel_fenced IS TRUE) "
            "OR (owner_runtime = 'render' AND vercel_fenced IS TRUE) "
            "OR (owner_runtime = 'vercel' AND render_fenced IS TRUE)",
            name="ck_cutover_global_writer_authority_safe_state",
        ),
        sa.CheckConstraint(
            "epoch >= 0",
            name="ck_cutover_global_writer_authority_epoch",
        ),
        sa.PrimaryKeyConstraint(
            "authority_key",
            name="pk_cutover_global_writer_authority",
        ),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO cutover_global_writer_authority (
                authority_key,
                owner_runtime,
                epoch,
                render_fenced,
                vercel_fenced
            ) VALUES (
                :authority_key,
                NULL,
                0,
                TRUE,
                TRUE
            )
            """
        ).bindparams(authority_key=AUTHORITY_KEY)
    )


def downgrade() -> None:
    op.drop_table(TABLE_NAME)
