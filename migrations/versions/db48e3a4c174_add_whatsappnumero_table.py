"""add WhatsappNumero table

Revision ID: db48e3a4c174
Revises: 32988b1d0996
Create Date: 2025-07-08 14:37:12.950007

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'db48e3a4c174'
down_revision = '32988b1d0996'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'whatsapp_numero',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('numero_whatsapp', sa.String(length=25), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True)
    )
    op.create_index(
        'ix_whatsapp_numero_numero_whatsapp',
        'whatsapp_numero',
        ['numero_whatsapp'],
        unique=True
    )

def downgrade():
    op.drop_index('ix_whatsapp_numero_numero_whatsapp', table_name='whatsapp_numero')
    op.drop_table('whatsapp_numero')
