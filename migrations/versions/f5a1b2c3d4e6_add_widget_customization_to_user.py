"""add widget customization fields to user

Revision ID: f5a1b2c3d4e6
Revises: d6709e14f414
Create Date: 2025-10-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f5a1b2c3d4e6'
down_revision = 'd6709e14f414'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('widget_icon_url', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('widget_animation', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('widget_animation')
        batch_op.drop_column('widget_icon_url')
