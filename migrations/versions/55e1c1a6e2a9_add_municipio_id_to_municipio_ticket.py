"""Add municipio_id to MunicipioTicket"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '55e1c1a6e2a9'
down_revision = 'ee3694e2e48a'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('municipio_ticket', sa.Column('municipio_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('municipio_ticket', 'municipio_id')
