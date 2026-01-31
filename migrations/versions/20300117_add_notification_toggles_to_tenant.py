"""Add notification toggles to TenantProfile

Revision ID: 20300117_add_notification_toggles
Revises: 20300116_add_tenant_and_idempotency
Create Date: 2030-01-17 10:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300117_add_notification_toggles'
down_revision = '20300116_add_tenant_and_idempotency'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('tenant_profile', schema=None) as batch_op:
        batch_op.add_column(sa.Column('send_buyer_email', sa.Boolean(), server_default='true', nullable=False))
        batch_op.add_column(sa.Column('send_dispatch_email', sa.Boolean(), server_default='true', nullable=False))
        batch_op.add_column(sa.Column('send_dispatch_whatsapp', sa.Boolean(), server_default='true', nullable=False))

def downgrade():
    with op.batch_alter_table('tenant_profile', schema=None) as batch_op:
        batch_op.drop_column('send_dispatch_whatsapp')
        batch_op.drop_column('send_dispatch_email')
        batch_op.drop_column('send_buyer_email')
