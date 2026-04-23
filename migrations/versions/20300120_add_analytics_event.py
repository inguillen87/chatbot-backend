"""Add analytics_event table

Revision ID: 20300120_add_analytics_event
Revises: 20300119_merge_all_heads
Create Date: 2026-01-30 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

# revision identifiers, used by Alembic.
revision = '20300120'
down_revision = '20300119_merge_catalog_and_order_event_heads'
branch_labels = None
depends_on = None


def upgrade():
    # Helper for JSON type
    conn = op.get_bind()
    if conn.dialect.name == 'postgresql':
        json_type = postgresql.JSONB
    else:
        json_type = sqlite.JSON

    op.create_table('analytics_event',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('target_type', sa.String(length=20), nullable=True),
        sa.Column('channel', sa.String(length=50), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.String(length=100), nullable=True),
        sa.Column('ticket_id', sa.Integer(), nullable=True),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lng', sa.Float(), nullable=True),
        sa.Column('geohash', sa.String(length=12), nullable=True),
        sa.Column('payload', json_type, nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_analytics_event_event_type'), 'analytics_event', ['event_type'], unique=False)
    op.create_index(op.f('ix_analytics_event_geohash'), 'analytics_event', ['geohash'], unique=False)
    op.create_index(op.f('ix_analytics_event_tenant_id'), 'analytics_event', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_analytics_event_timestamp'), 'analytics_event', ['timestamp'], unique=False)
    op.create_index('ix_analytics_event_tenant_ts', 'analytics_event', ['tenant_id', 'timestamp'], unique=False)
    op.create_index('ix_analytics_event_tenant_type', 'analytics_event', ['tenant_id', 'event_type'], unique=False)


def downgrade():
    op.drop_index('ix_analytics_event_tenant_type', table_name='analytics_event')
    op.drop_index('ix_analytics_event_tenant_ts', table_name='analytics_event')
    op.drop_index(op.f('ix_analytics_event_timestamp'), table_name='analytics_event')
    op.drop_index(op.f('ix_analytics_event_tenant_id'), table_name='analytics_event')
    op.drop_index(op.f('ix_analytics_event_geohash'), table_name='analytics_event')
    op.drop_index(op.f('ix_analytics_event_event_type'), table_name='analytics_event')
    op.drop_table('analytics_event')
