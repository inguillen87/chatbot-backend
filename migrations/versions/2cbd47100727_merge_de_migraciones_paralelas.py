"""Merge de migraciones paralelas

Revision ID: 2cbd47100727
Revises: 063947b34b71, b2c3d4e5f6a7
Create Date: 2025-08-07 00:21:34.771701

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2cbd47100727'
down_revision = ('063947b34b71', 'b2c3d4e5f6a7')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
