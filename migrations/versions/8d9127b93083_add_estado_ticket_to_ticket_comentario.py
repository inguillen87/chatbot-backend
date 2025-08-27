"""add estado_ticket to ticket_comentario"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '8d9127b93083'
down_revision = '071dc98a431a'
branch_labels = None
depends_on = None


def _has_column(bind, table, column):
    insp = sa.inspect(bind)
    return column in [c['name'] for c in insp.get_columns(table)]


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind, 'ticket_comentario', 'estado_ticket'):
        op.add_column(
            'ticket_comentario',
            sa.Column('estado_ticket', sa.String(length=30), nullable=True)
        )


def downgrade():
    bind = op.get_bind()
    if _has_column(bind, 'ticket_comentario', 'estado_ticket'):
        # Alembic cannot drop columns on SQLite easily; leave as no-op.
        pass
