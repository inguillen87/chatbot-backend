"""merge branches

Revision ID: 57e48e92bbbe
Revises: 68b10e799a0d, 2cde27e7dacb
Create Date: 2025-06-19 10:17:03.218740

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '57e48e92bbbe'
down_revision = ('68b10e799a0d', '2cde27e7dacb')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
