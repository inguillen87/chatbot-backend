"""Merge tenant profile head revisions.

Revision ID: 20300111_merge_tenant_profile_heads
Revises: 20300108_add_tenant_is_active, 20300110_add_whatsapp_sender_to_tenant_profile
Create Date: 2030-01-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300111_merge_tenant_profile_heads'
down_revision = ('20300108_add_tenant_is_active', '20300110_add_whatsapp_sender_to_tenant_profile')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
