"""Add disponible column to catalogo_item

Revision ID: 20261206_add_disponible_to_catalogo_item
Revises: 20261205_add_whatsapp_sender_id_to_tenant_profile
Create Date: 2026-12-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20261206_add_disponible_to_catalogo_item'
down_revision = '20261205_add_whatsapp_sender_id_to_tenant_profile'
branch_labels = None
depends_on = None


def upgrade():
    # Add 'disponible' column to 'catalogo_item'
    # We use server_default='true' to ensure existing rows default to available
    op.add_column('catalogo_item', sa.Column('disponible', sa.Boolean(), nullable=False, server_default='true'))

    # Optional: remove server_default if you don't want it to persist for new rows
    # (though model has default=True python-side)
    # op.alter_column('catalogo_item', 'disponible', server_default=None)


def downgrade():
    op.drop_column('catalogo_item', 'disponible')
