"""Add IntegrationEvent and update MarketOrder

Revision ID: 20300105_update_market_models
Revises: 20300104_add_integration_and_notification_models
Create Date: 2030-01-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300105_update_market_models'
down_revision = '20300104_add_integration_and_notification_models'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # IntegrationEvent
    op.create_table('integration_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('event_id', sa.String(length=120), nullable=False),
        sa.Column('event_type', sa.String(length=80), nullable=True),
        sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB, 'postgresql'), nullable=True),
        sa.Column('processed', sa.Boolean(), server_default='false', nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'provider', 'event_id', name='uq_integration_event_dedupe')
    )
    op.create_index(op.f('ix_integration_event_tenant_id'), 'integration_event', ['tenant_id'], unique=False)

    # MarketOrder columns
    with op.batch_alter_table('market_order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('channel', sa.String(length=50), server_default='web', nullable=True))
        batch_op.add_column(sa.Column('contact_email', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('note', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('external_provider', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('external_order_id', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('external_url', sa.String(length=500), nullable=True))
        batch_op.create_index('ix_market_order_external', ['tenant_id', 'external_provider', 'external_order_id'], unique=False)

def downgrade() -> None:
    with op.batch_alter_table('market_order', schema=None) as batch_op:
        batch_op.drop_index('ix_market_order_external')
        batch_op.drop_column('external_url')
        batch_op.drop_column('external_order_id')
        batch_op.drop_column('external_provider')
        batch_op.drop_column('note')
        batch_op.drop_column('contact_email')
        batch_op.drop_column('channel')

    op.drop_index(op.f('ix_integration_event_tenant_id'), table_name='integration_event')
    op.drop_table('integration_event')
