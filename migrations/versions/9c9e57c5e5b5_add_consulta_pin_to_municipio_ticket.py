"""add consulta_pin to municipio_ticket"""

from alembic import op
import sqlalchemy as sa
import random

# revision identifiers, used by Alembic.
revision = '9c9e57c5e5b5'
down_revision = '8d9127b93083'
branch_labels = None
depends_on = None


def _has_column(bind, table, column):
    insp = sa.inspect(bind)
    return column in [c['name'] for c in insp.get_columns(table)]


def upgrade():
    bind = op.get_bind()
    if not _has_column(bind, 'municipio_ticket', 'consulta_pin'):
        op.add_column('municipio_ticket', sa.Column('consulta_pin', sa.String(length=6), nullable=True))

        # Populate existing rows with random pins
        municipio_ticket = sa.table(
            'municipio_ticket',
            sa.column('id', sa.Integer),
            sa.column('consulta_pin', sa.String(length=6))
        )
        res = bind.execute(sa.select(municipio_ticket.c.id)).fetchall()
        for (ticket_id,) in res:
            bind.execute(
                sa.update(municipio_ticket)
                .where(municipio_ticket.c.id == ticket_id)
                .values(consulta_pin=f"{random.randint(100000, 999999)}")
            )

        op.alter_column('municipio_ticket', 'consulta_pin', nullable=False)


def downgrade():
    bind = op.get_bind()
    if _has_column(bind, 'municipio_ticket', 'consulta_pin'):
        # Column removal is a no-op for SQLite compatibility
        pass

