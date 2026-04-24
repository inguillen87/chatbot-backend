"""Make whatsapp_sender_id unique in TenantProfile

Revision ID: 20300107_make_whatsapp_sender_id_unique
Revises: 20300106_add_catalog_checkout_fields
Create Date: 2030-01-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300107_make_whatsapp_sender_id_unique'
down_revision = '20300106_add_catalog_checkout_fields'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Ensure uniqueness for routing
    with op.batch_alter_table('tenant_profile', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_tenant_profile_whatsapp_sender_id', ['whatsapp_sender_id'])
        batch_op.create_index(batch_op.f('ix_tenant_profile_whatsapp_sender_id'), ['whatsapp_sender_id'], unique=True)

def downgrade() -> None:
    with op.batch_alter_table('tenant_profile', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_profile_whatsapp_sender_id'))
        batch_op.drop_constraint('uq_tenant_profile_whatsapp_sender_id', type_='unique')
