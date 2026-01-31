"""Merge all heads

Revision ID: 20300119_merge_all_heads
Revises: 20300118_add_order_event_table, 20250210_add_features_table, 20261205_add_whatsapp_sender_id_to_tenant_profile, 20280415_add_live_voting_flags_to_enc_encuesta, 20300108_add_tenant_is_active, 20260405_add_webauthn_support, 20251210_merge_public_survey_password_reset_heads, 20251008b2c3, 1f2b3c4d5e6f, 20280416_merge_live_voting_and_whatsapp_heads, c4c3d9d3f88b, 20300110_add_whatsapp_sender_to_tenant_profile, 202511210001, 20260601_add_tenant_id_to_catalogo_item, 1d4c9d7c5a8f
Create Date: 2026-01-30 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300119_merge_all_heads'
down_revision = (
    '20300118_add_order_event_table',
    '20250210_add_features_table',
    '20261205_add_whatsapp_sender_id_to_tenant_profile',
    '20280415_add_live_voting_flags_to_enc_encuesta',
    '20300108_add_tenant_is_active',
    '20260405_add_webauthn_support',
    '20251210_merge_public_survey_password_reset_heads',
    '20251008b2c3',
    '1f2b3c4d5e6f',
    '20280416_merge_live_voting_and_whatsapp_heads',
    'c4c3d9d3f88b',
    '20300110_add_whatsapp_sender_to_tenant_profile',
    '202511210001',
    '20260601_add_tenant_id_to_catalogo_item',
    '1d4c9d7c5a8f'
)
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
