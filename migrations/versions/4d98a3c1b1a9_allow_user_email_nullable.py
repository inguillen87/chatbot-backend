"""allow user email to be nullable"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '4d98a3c1b1a9'
down_revision = '3f3b6a9b7d6e'
branch_labels = None
depends_on = None

def upgrade():
    op.alter_column('user', 'email',
               existing_type=sa.String(length=120),
               nullable=True)

def downgrade():
    op.alter_column('user', 'email',
               existing_type=sa.String(length=120),
               nullable=False)
