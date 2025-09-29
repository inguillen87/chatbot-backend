"""add analytics module tables

Revision ID: 1f2b3c4d5e6f
Revises: b236fbe99d2d
Create Date: 2024-03-21 00:00:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1f2b3c4d5e6f'
down_revision = 'b236fbe99d2d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'analytics_daily_metric',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('metric_date', sa.Date(), nullable=False),
        sa.Column('metric', sa.String(length=64), nullable=False),
        sa.Column('dimension', sa.String(length=128), nullable=True),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('extra', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'scope', 'metric_date', 'metric', 'dimension', name='uq_analytics_daily_metric')
    )
    op.create_index(op.f('ix_analytics_daily_metric_metric'), 'analytics_daily_metric', ['metric'], unique=False)
    op.create_index(op.f('ix_analytics_daily_metric_metric_date'), 'analytics_daily_metric', ['metric_date'], unique=False)
    op.create_index(op.f('ix_analytics_daily_metric_scope'), 'analytics_daily_metric', ['scope'], unique=False)
    op.create_index(op.f('ix_analytics_daily_metric_tenant_id'), 'analytics_daily_metric', ['tenant_id'], unique=False)

    op.create_table(
        'analytics_geo_cell',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('metric_date', sa.Date(), nullable=False),
        sa.Column('cell_id', sa.String(length=32), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('severity_avg', sa.Float(), nullable=True),
        sa.Column('categories', sa.JSON(), nullable=True),
        sa.Column('centroid', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'scope', 'metric_date', 'cell_id', name='uq_analytics_geo_cell')
    )
    op.create_index(op.f('ix_analytics_geo_cell_metric_date'), 'analytics_geo_cell', ['metric_date'], unique=False)
    op.create_index(op.f('ix_analytics_geo_cell_scope'), 'analytics_geo_cell', ['scope'], unique=False)
    op.create_index(op.f('ix_analytics_geo_cell_tenant_id'), 'analytics_geo_cell', ['tenant_id'], unique=False)

    op.create_table(
        'analytics_top_metric',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=32), nullable=False),
        sa.Column('metric_date', sa.Date(), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=128), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('delta', sa.Float(), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'scope', 'metric_date', 'category', 'label', name='uq_analytics_top_metric')
    )
    op.create_index(op.f('ix_analytics_top_metric_metric_date'), 'analytics_top_metric', ['metric_date'], unique=False)
    op.create_index(op.f('ix_analytics_top_metric_scope'), 'analytics_top_metric', ['scope'], unique=False)
    op.create_index(op.f('ix_analytics_top_metric_tenant_id'), 'analytics_top_metric', ['tenant_id'], unique=False)

    op.create_table(
        'analytics_cohort_metric',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('cohort_key', sa.String(length=64), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('retention_30', sa.Float(), nullable=True),
        sa.Column('retention_60', sa.Float(), nullable=True),
        sa.Column('retention_90', sa.Float(), nullable=True),
        sa.Column('extra', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'cohort_key', name='uq_analytics_cohort_metric')
    )
    op.create_index(op.f('ix_analytics_cohort_metric_tenant_id'), 'analytics_cohort_metric', ['tenant_id'], unique=False)

    op.create_table(
        'analytics_module_status',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('cache_hits', sa.Integer(), nullable=False),
        sa.Column('cache_misses', sa.Integer(), nullable=False),
        sa.Column('cache_evictions', sa.Integer(), nullable=False),
        sa.Column('jobs_pending', sa.Integer(), nullable=False),
        sa.Column('jobs_running', sa.Integer(), nullable=False),
        sa.Column('jobs_failed', sa.Integer(), nullable=False),
        sa.Column('extra', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'analytics_whatsapp_template',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('template_name', sa.String(length=128), nullable=False),
        sa.Column('metric_date', sa.Date(), nullable=False),
        sa.Column('sent', sa.Integer(), nullable=False),
        sa.Column('delivered', sa.Integer(), nullable=False),
        sa.Column('read', sa.Integer(), nullable=False),
        sa.Column('responded', sa.Integer(), nullable=False),
        sa.Column('blocked', sa.Integer(), nullable=False),
        sa.Column('extra', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'template_name', 'metric_date', name='uq_analytics_whatsapp_template')
    )
    op.create_index(op.f('ix_analytics_whatsapp_template_metric_date'), 'analytics_whatsapp_template', ['metric_date'], unique=False)
    op.create_index(op.f('ix_analytics_whatsapp_template_tenant_id'), 'analytics_whatsapp_template', ['tenant_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_analytics_whatsapp_template_tenant_id'), table_name='analytics_whatsapp_template')
    op.drop_index(op.f('ix_analytics_whatsapp_template_metric_date'), table_name='analytics_whatsapp_template')
    op.drop_table('analytics_whatsapp_template')
    op.drop_table('analytics_module_status')
    op.drop_index(op.f('ix_analytics_cohort_metric_tenant_id'), table_name='analytics_cohort_metric')
    op.drop_table('analytics_cohort_metric')
    op.drop_index(op.f('ix_analytics_top_metric_tenant_id'), table_name='analytics_top_metric')
    op.drop_index(op.f('ix_analytics_top_metric_scope'), table_name='analytics_top_metric')
    op.drop_index(op.f('ix_analytics_top_metric_metric_date'), table_name='analytics_top_metric')
    op.drop_table('analytics_top_metric')
    op.drop_index(op.f('ix_analytics_geo_cell_tenant_id'), table_name='analytics_geo_cell')
    op.drop_index(op.f('ix_analytics_geo_cell_scope'), table_name='analytics_geo_cell')
    op.drop_index(op.f('ix_analytics_geo_cell_metric_date'), table_name='analytics_geo_cell')
    op.drop_table('analytics_geo_cell')
    op.drop_index(op.f('ix_analytics_daily_metric_tenant_id'), table_name='analytics_daily_metric')
    op.drop_index(op.f('ix_analytics_daily_metric_scope'), table_name='analytics_daily_metric')
    op.drop_index(op.f('ix_analytics_daily_metric_metric_date'), table_name='analytics_daily_metric')
    op.drop_index(op.f('ix_analytics_daily_metric_metric'), table_name='analytics_daily_metric')
    op.drop_table('analytics_daily_metric')
