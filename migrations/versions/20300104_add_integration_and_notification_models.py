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

def downgrade() -> None:
    with op.batch_alter_table('public_survey', schema=None) as batch_op:
        batch_op.drop_constraint('fk_public_survey_tenant_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_public_survey_tenant_id'))
        batch_op.drop_column('tenant_id')

    op.drop_index(op.f('ix_notification_log_tenant_id'), table_name='notification_log')
    op.drop_table('notification_log')
    op.drop_index(op.f('ix_integration_account_tenant_id'), table_name='integration_account')
    op.drop_table('integration_account')
