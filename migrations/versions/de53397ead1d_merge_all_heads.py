"""merge all heads

Revision ID: de53397ead1d
Revises: 20260224_add_report_count_to_enc_comentario, 20260401_be04_whatsapp_contact_state
Create Date: 2026-04-25 23:38:09.856369

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'de53397ead1d'
down_revision = ('20260224_add_report_count_to_enc_comentario', '20260401_be04_whatsapp_contact_state')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
