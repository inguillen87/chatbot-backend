"""add password reset columns to user

Revision ID: 20251201_password_reset_security
Revises: 20251116_enc_respuesta_metadata
Create Date: 2025-12-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20251201_password_reset_security"
down_revision = "20251116_enc_respuesta_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("password_reset_selector", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("password_reset_verifier_hash", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("password_reset_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_user_password_reset_selector",
        "user",
        ["password_reset_selector"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_user_password_reset_selector", table_name="user")
    op.drop_column("user", "password_reset_sent_at")
    op.drop_column("user", "password_reset_verifier_hash")
    op.drop_column("user", "password_reset_selector")
