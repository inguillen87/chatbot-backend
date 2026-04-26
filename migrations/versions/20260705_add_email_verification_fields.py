"""Add email verification fields to user table

Revision ID: 20260705_add_email_verification_fields
Revises: 20260630_add_ticket_assignment_fields
Create Date: 2026-07-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260705_add_email_verification_fields"
down_revision = "20260630_add_ticket_assignment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "email_verification_token",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "email_verification_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_user_email_verification_token"),
        "user",
        ["email_verification_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_email_verification_token"), table_name="user")
    op.drop_column("user", "email_verification_sent_at")
    op.drop_column("user", "email_verification_token")
    op.drop_column("user", "email_verified")
