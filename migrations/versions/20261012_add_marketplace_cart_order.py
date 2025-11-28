"""
Add marketplace cart and order tables

Revision ID: 20261012_add_marketplace_cart_order
Revises: 20260918_add_entity_token_and_tenant_slug
Create Date: 2026-10-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20261012_add_marketplace_cart_order"
down_revision = "20260918_add_entity_token_and_tenant_slug"
branch_labels = None
depends_on = None


def _json_type(bind):
    if bind and bind.dialect.name != "sqlite":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    json_type = _json_type(bind)

    op.create_table(
        "market_cart",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("contact_name", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=50), nullable=True),
        sa.Column("metadata", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_market_cart_tenant_session",
        "market_cart",
        ["tenant_id", "session_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_market_cart_tenant_id",
        "market_cart",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_market_cart_user_id",
        "market_cart",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_market_cart_session_id",
        "market_cart",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "market_cart_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cart_id",
            sa.Integer(),
            sa.ForeignKey("market_cart.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("catalogo_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("price_text", sa.String(length=100), nullable=True),
        sa.Column("price_monetary", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_points", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("modalidad", sa.String(length=20), nullable=True),
        sa.Column("name_snapshot", sa.String(length=255), nullable=True),
        sa.Column("extra", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_market_cart_item_cart_id",
        "market_cart_item",
        ["cart_id"],
        unique=False,
    )
    op.create_index(
        "ix_market_cart_item_product_id",
        "market_cart_item",
        ["product_id"],
        unique=False,
    )

    op.create_table(
        "market_order",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column(
            "cart_id",
            sa.Integer(),
            sa.ForeignKey("market_cart.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("contact_name", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=50), nullable=True),
        sa.Column("total_monetary", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_points", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("metadata", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_market_order_tenant_id",
        "market_order",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_market_order_user_id",
        "market_order",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_market_order_cart_id",
        "market_order",
        ["cart_id"],
        unique=False,
    )

    op.create_table(
        "market_order_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("market_order.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("catalogo_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("price_monetary", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_points", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("modalidad", sa.String(length=20), nullable=True),
        sa.Column("name_snapshot", sa.String(length=255), nullable=True),
        sa.Column("extra", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_market_order_item_order_id",
        "market_order_item",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        "ix_market_order_item_product_id",
        "market_order_item",
        ["product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_market_order_item_product_id", table_name="market_order_item")
    op.drop_index("ix_market_order_item_order_id", table_name="market_order_item")
    op.drop_table("market_order_item")
    op.drop_index("ix_market_order_cart_id", table_name="market_order")
    op.drop_index("ix_market_order_user_id", table_name="market_order")
    op.drop_index("ix_market_order_tenant_id", table_name="market_order")
    op.drop_table("market_order")
    op.drop_index("ix_market_cart_item_product_id", table_name="market_cart_item")
    op.drop_index("ix_market_cart_item_cart_id", table_name="market_cart_item")
    op.drop_table("market_cart_item")
    op.drop_index("ix_market_cart_session_id", table_name="market_cart")
    op.drop_index("ix_market_cart_user_id", table_name="market_cart")
    op.drop_index("ix_market_cart_tenant_id", table_name="market_cart")
    op.drop_index("ix_market_cart_tenant_session", table_name="market_cart")
    op.drop_table("market_cart")
