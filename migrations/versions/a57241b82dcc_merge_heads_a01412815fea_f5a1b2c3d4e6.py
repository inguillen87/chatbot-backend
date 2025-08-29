"""merge heads a01412815fea+f5a1b2c3d4e6

Revision ID: a57241b82dcc
Revises: a01412815fea, f5a1b2c3d4e6
Create Date: 2025-08-29 00:24:43.477656

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a57241b82dcc'
down_revision = ('a01412815fea', 'f5a1b2c3d4e6')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
