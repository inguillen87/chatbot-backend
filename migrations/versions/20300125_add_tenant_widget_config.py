"""add tenant widget config

Revision ID: 20300125
Revises: 20300124
Create Date: 2030-01-25 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = '20300125'
down_revision = '20300124'
branch_labels = None
depends_on = None


def upgrade():
    # Detect if we are running on SQLite to use the correct JSON type
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'

    json_type = sa.JSON() if not is_sqlite else sqlite.JSON()
    if bind.dialect.name == 'postgresql':
        json_type = postgresql.JSONB(astext_type=sa.Text())

    op.create_table('tenant_widget_config',
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('config_live', json_type, nullable=False),
        sa.Column('config_draft', json_type, nullable=False),
        sa.Column('last_published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('published_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['published_by'], ['user.id'], ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tenant_widget_config', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenant_widget_config_tenant_id'), ['tenant_id'], unique=True)


def downgrade():
    with op.batch_alter_table('tenant_widget_config', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_widget_config_tenant_id'))

    op.drop_table('tenant_widget_config')
