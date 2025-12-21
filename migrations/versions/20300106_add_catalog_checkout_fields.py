"""Add checkout_type and external_url to CatalogoItem

Revision ID: 20300106_add_catalog_checkout_fields
Revises: 20300105_update_market_models
Create Date: 2030-01-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300106_add_catalog_checkout_fields'
down_revision = '20300105_update_market_models'
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table('catalogo_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('checkout_type', sa.String(length=50), server_default='chatboc', nullable=True))
        batch_op.add_column(sa.Column('external_url', sa.String(length=500), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table('catalogo_item', schema=None) as batch_op:
        batch_op.drop_column('external_url')
        batch_op.drop_column('checkout_type')
