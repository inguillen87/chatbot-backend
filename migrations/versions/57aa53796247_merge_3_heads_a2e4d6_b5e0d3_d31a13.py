"""merge 3 heads (a2e4d6, b5e0d3, d31a13)

Revision ID: 57aa53796247
Revises: a2e4d6d50712, b5e0d3f2c1a4, d31a13125cc3
Create Date: 2025-08-14 14:00:26.536997

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '57aa53796247'
down_revision = ('a2e4d6d50712', 'b5e0d3f2c1a4', 'd31a13125cc3')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
