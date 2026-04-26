from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b1f2a4a25d3e'
down_revision = '4d98a3c1b1a9'
branch_labels = None
depends_on = None

def upgrade():
    op.create_index('ix_user_empresa_telefono', 'user', ['empresa_id', 'telefono'])
    op.create_unique_constraint('uq_user_empresa_telefono', 'user', ['empresa_id', 'telefono'])


def downgrade():
    op.drop_constraint('uq_user_empresa_telefono', 'user', type_='unique')
    op.drop_index('ix_user_empresa_telefono', table_name='user')
