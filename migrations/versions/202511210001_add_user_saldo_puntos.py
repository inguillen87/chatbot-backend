"""Add saldo_puntos column to user

Revision ID: 202511210001
Revises: 20260406_merge_webauthn_public_survey_heads
Create Date: 2025-11-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '202511210001'
down_revision = '20260406_merge_webauthn_public_survey_heads'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column(
        'user',
        sa.Column('saldo_puntos', sa.Integer(), nullable=False, server_default='0'),
    )
    op.alter_column('user', 'saldo_puntos', server_default=None)


def downgrade():
    op.drop_column('user', 'saldo_puntos')
