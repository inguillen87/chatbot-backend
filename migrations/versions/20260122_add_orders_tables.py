"""Add orders table and catalog preview fields

Revision ID: 20300122_add_orders_tables
Revises: 20300121_add_dispatch_notification_config
Create Date: 2030-01-22 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300122'
down_revision = '20300121'
branch_labels = None
depends_on = None


def upgrade():
    # Helper for JSON type
    json_type = sa.JSON()
    # Attempt to use JSONB if on Postgres, though SQLAlchemy usually abstracts this if configured correctly
    # We'll rely on the model definition mapping but for raw SQL operations here usually standard types are fine.

    # 1. Create orders table
    op.create_table('orders',
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('buyer_name', sa.String(length=255), nullable=True),
        sa.Column('buyer_email', sa.String(length=255), nullable=True),
        sa.Column('buyer_phone', sa.String(length=50), nullable=True),
        sa.Column('buyer_notes', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('channel', sa.String(length=50), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=True),
        sa.Column('subtotal', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('discount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('shipping_cost', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('total', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('delivery_address', json_type, nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['user.id'], ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_orders_customer_id'), 'orders', ['customer_id'], unique=False)
    op.create_index(op.f('ix_orders_status'), 'orders', ['status'], unique=False)
    op.create_index(op.f('ix_orders_tenant_id'), 'orders', ['tenant_id'], unique=False)

    # 2. Create order_items table
    op.create_table('order_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.String(length=36), nullable=False),
        sa.Column('catalog_item_id', sa.Integer(), nullable=True),
        sa.Column('sku', sa.String(length=100), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('total_price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('meta_data', json_type, nullable=True),
        sa.ForeignKeyConstraint(['catalog_item_id'], ['catalogo_item.id'], ),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_order_items_order_id'), 'order_items', ['order_id'], unique=False)

    # 3. Add columns to catalog_upload
    with op.batch_alter_table('catalog_upload', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preview_data', json_type, nullable=True))
        batch_op.add_column(sa.Column('warnings', json_type, nullable=True))


def downgrade():
    with op.batch_alter_table('catalog_upload', schema=None) as batch_op:
        batch_op.drop_column('warnings')
        batch_op.drop_column('preview_data')

    op.drop_table('order_items')
    op.drop_table('orders')
