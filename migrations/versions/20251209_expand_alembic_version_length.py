"""Expand alembic_version.version_num column length."""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20251209_expand_ver_len"
down_revision = "20251201_password_reset_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=128),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
