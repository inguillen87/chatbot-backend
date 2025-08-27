"""merge heads: timeline + consulta_pin

Revision ID: a01412815fea
Revises: 2cd1bd3fa7dd, 9c9e57c5e5b5
Create Date: 2025-08-27 18:13:09.732579

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a01412815fea'
down_revision = ('2cd1bd3fa7dd', '9c9e57c5e5b5')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
