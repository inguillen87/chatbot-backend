"""Merge all heads

Revision ID: 4a6b01a3eed4
Revises: 20260224_add_report_count_to_enc_comentario, 20260401_be04_whatsapp_contact_state
Create Date: 2026-04-24 00:07:12.235930

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4a6b01a3eed4'
down_revision = ('20260224_add_report_count_to_enc_comentario', '20260401_be04_whatsapp_contact_state')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
