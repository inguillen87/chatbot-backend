"""Add IntegrationAccount and NotificationLog models

Revision ID: 20300104_add_integration_and_notification_models
Revises: 20300103_ensure_schema_postgres
Create Date: 2030-01-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300104_add_integration_and_notification_models'
down_revision = '20300103_ensure_schema_postgres'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # IntegrationAccount
    op.create_table('integration_account',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=50), nullable=False),
        sa.Column('credentials', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=True),
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_integration_account_tenant_id'), 'integration_account', ['tenant_id'], unique=False)

    # NotificationLog
    op.create_table('notification_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('recipient', sa.String(length=255), nullable=False),
        sa.Column('message_type', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='sent', nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notification_log_tenant_id'), 'notification_log', ['tenant_id'], unique=False)

    # PublicSurvey - Add tenant_id
    with op.batch_alter_table('public_survey', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_public_survey_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_foreign_key('fk_public_survey_tenant_id', 'tenant_profile', ['tenant_id'], ['id'])

    # MarketCart
    op.create_table('market_cart',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('session_id', sa.String(length=120), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
        sa.Column('contact_name', sa.String(length=255), nullable=True),
        sa.Column('contact_phone', sa.String(length=50), nullable=True),
        sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_market_cart_tenant_session'), 'market_cart', ['tenant_id', 'session_id', 'status'], unique=False)

    # MarketCartItem
    op.create_table('market_cart_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cart_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), server_default='1', nullable=False),
        sa.Column('price_text', sa.String(length=100), nullable=True),
        sa.Column('price_monetary', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('price_points', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=True),
        sa.Column('modalidad', sa.String(length=20), nullable=True),
        sa.Column('name_snapshot', sa.String(length=255), nullable=True),
        sa.Column('extra', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['cart_id'], ['market_cart.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['catalogo_item.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_market_cart_item_cart_id'), 'market_cart_item', ['cart_id'], unique=False)
    op.create_index(op.f('ix_market_cart_item_product_id'), 'market_cart_item', ['product_id'], unique=False)

    # MarketOrder
    op.create_table('market_order',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('cart_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('contact_name', sa.String(length=255), nullable=True),
        sa.Column('contact_phone', sa.String(length=50), nullable=True),
        sa.Column('total_monetary', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('total_points', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=True),
        sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['cart_id'], ['market_cart.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_market_order_cart_id'), 'market_order', ['cart_id'], unique=False)
    op.create_index(op.f('ix_market_order_tenant_id'), 'market_order', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_market_order_user_id'), 'market_order', ['user_id'], unique=False)

    # MarketOrderItem
    op.create_table('market_order_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), server_default='1', nullable=False),
        sa.Column('price_monetary', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('price_points', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=True),
        sa.Column('modalidad', sa.String(length=20), nullable=True),
        sa.Column('name_snapshot', sa.String(length=255), nullable=True),
        sa.Column('extra', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['market_order.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['catalogo_item.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_market_order_item_order_id'), 'market_order_item', ['order_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_market_order_item_order_id'), table_name='market_order_item')
    op.drop_table('market_order_item')
    op.drop_index(op.f('ix_market_order_user_id'), table_name='market_order')
    op.drop_index(op.f('ix_market_order_tenant_id'), table_name='market_order')
    op.drop_index(op.f('ix_market_order_cart_id'), table_name='market_order')
    op.drop_table('market_order')
    op.drop_index(op.f('ix_market_cart_item_product_id'), table_name='market_cart_item')
    op.drop_index(op.f('ix_market_cart_item_cart_id'), table_name='market_cart_item')
    op.drop_table('market_cart_item')
    op.drop_index(op.f('ix_market_cart_tenant_session'), table_name='market_cart')
    op.drop_table('market_cart')

    with op.batch_alter_table('public_survey', schema=None) as batch_op:
        batch_op.drop_constraint('fk_public_survey_tenant_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_public_survey_tenant_id'))
        batch_op.drop_column('tenant_id')

    op.drop_index(op.f('ix_notification_log_tenant_id'), table_name='notification_log')
    op.drop_table('notification_log')
    op.drop_index(op.f('ix_integration_account_tenant_id'), table_name='integration_account')
    op.drop_table('integration_account')
