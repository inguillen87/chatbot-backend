"""Add widget_settings cta_messages and theme_config

Revision ID: 20300102_add_widget_settings_columns
Revises: 20300101_add_disponible_to_catalogo_item
Create Date: 2030-01-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300102_add_widget_settings_columns'
down_revision = '20300101_add_disponible_to_catalogo_item'
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    cols = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "widget_settings", "cta_messages"):
        if bind.dialect.name == "postgresql":
            # Use JSONB for Postgres
            op.add_column(
                "widget_settings",
                sa.Column("cta_messages", postgresql.JSONB, server_default='[]', nullable=True),
            )
        else:
            # Fallback for SQLite
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


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "widget_settings", "theme_config"):
        op.drop_column("widget_settings", "theme_config")

    if _has_column(inspector, "widget_settings", "cta_messages"):
        op.drop_column("widget_settings", "cta_messages")
