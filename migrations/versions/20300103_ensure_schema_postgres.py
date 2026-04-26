"""Ensure schema consistency for Postgres

Revision ID: 20300103_ensure_schema_postgres
Revises: 20300102_add_widget_settings_columns
Create Date: 2030-01-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300103_ensure_schema_postgres'
down_revision = '20300102_add_widget_settings_columns'
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    cols = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Ensure widget_settings columns exist (safety net)
    if not _has_column(inspector, "widget_settings", "cta_messages"):
        if bind.dialect.name == "postgresql":
            op.add_column(
                "widget_settings",
                sa.Column("cta_messages", postgresql.JSONB, server_default='[]', nullable=True),
            )
        else:
            op.add_column(
                "widget_settings",
                sa.Column("cta_messages", sa.JSON, server_default='[]', nullable=True),
            )

    if not _has_column(inspector, "widget_settings", "theme_config"):
        if bind.dialect.name == "postgresql":
            op.add_column(
                "widget_settings",
                sa.Column("theme_config", postgresql.JSONB, server_default='{}', nullable=True),
            )
        else:
            op.add_column(
                "widget_settings",
                sa.Column("theme_config", sa.JSON, server_default='{}', nullable=True),
            )

    # Ensure catalogo_item columns exist (safety net)
    if not _has_column(inspector, "catalogo_item", "disponible"):
        op.add_column("catalogo_item", sa.Column("disponible", sa.Boolean(), server_default=sa.text("true"), nullable=True))


def downgrade() -> None:
    pass
