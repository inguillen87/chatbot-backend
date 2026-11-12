"""manage the server-side Flask session table through Alembic

Revision ID: 20260906_flask_sessions_v1
Revises: 20260905_municipio_handoff_v1
Create Date: 2026-09-06 02:00:00.000000

The application previously created this table opportunistically during every
process start.  The migration is intentionally idempotent because established
environments already have that runtime-created table.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260906_flask_sessions_v1"
down_revision = "20260905_municipio_handoff_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("flask_sessions"):
        return

    op.create_table(
        "flask_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("expiry", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )


def downgrade() -> None:
    # Preserve established runtime-created session tables and active logins.
    # Removing the revision marker does not require destructive data loss.
    pass
