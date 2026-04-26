"""add is_active to tenant_profile

Revision ID: 20300108_add_tenant_is_active
Revises: 20300107_make_whatsapp_sender_id_unique
Create Date: 2030-01-08 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '20300108_add_tenant_is_active'
down_revision = '20300107_make_whatsapp_sender_id_unique'
branch_labels = None
depends_on = None


def upgrade():
    # Attempt to add is_active column. Use batch_alter_table for SQLite compatibility if needed,
    # though straightforward add_column usually works for simple types.
    # We check existence in init_tenants.py but here we do standard migration.
    with op.batch_alter_table('tenant_profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'))


def downgrade():
    with op.batch_alter_table('tenant_profile', schema=None) as batch_op:
        batch_op.drop_column('is_active')
