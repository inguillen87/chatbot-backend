"""merge branches

Revision ID: 2cde27e7dacb
Revises: 7f7fa92ad18c, 7be0011e1eca
Create Date: 2025-06-19 10:16:24.977432

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2cde27e7dacb'
down_revision = ('7f7fa92ad18c', '7be0011e1eca')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
