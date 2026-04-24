"""Add analytics_events_v2 table

Revision ID: 20300124_add_analytics_v2
Revises: 20300123_add_theme_json
Create Date: 2030-01-24 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300124'
down_revision = '20300123'
branch_labels = None
depends_on = None

def upgrade():
    # Helper for JSON type
    json_type = sa.JSON()

    op.create_table('analytics_events_v2',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('tenant_type', sa.String(length=20), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('anon_id', sa.String(length=100), nullable=True),
        sa.Column('channel', sa.String(length=50), nullable=True),
        sa.Column('event_name', sa.String(length=100), nullable=False),
        sa.Column('session_id', sa.String(length=100), nullable=True),
        sa.Column('metadata', json_type, nullable=True),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lng', sa.Float(), nullable=True),
        sa.Column('entity_ref', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_index(op.f('ix_analytics_events_v2_tenant_id'), 'analytics_events_v2', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_analytics_events_v2_ts'), 'analytics_events_v2', ['ts'], unique=False)
    op.create_index(op.f('ix_analytics_events_v2_event_name'), 'analytics_events_v2', ['event_name'], unique=False)

def downgrade():
    op.drop_table('analytics_events_v2')
