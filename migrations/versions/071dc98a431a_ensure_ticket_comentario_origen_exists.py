"""ensure ticket_comentario.origen exists (idempotente)"""
from alembic import op
import sqlalchemy as sa

# Usa el ID real del archivo creado
revision = '071dc98a431a'
down_revision = '57aa53796247'  # tu merge head actual
branch_labels = None
depends_on = None

def _has_column(bind, table, column):
    insp = sa.inspect(bind)
    return column in [c['name'] for c in insp.get_columns(table)]

def upgrade():
    bind = op.get_bind()

    # 1) ticket_comentario.origen
    if not _has_column(bind, 'ticket_comentario', 'origen'):
        op.add_column(
            'ticket_comentario',
            sa.Column('origen', sa.String(length=20), nullable=True, server_default='chat')
        )
        # Si quieres dejarla NOT NULL sin pelea con SQLite, quita el default después:
        op.execute("UPDATE ticket_comentario SET origen='chat' WHERE origen IS NULL")

        # En SQLite, ALTER para NOT NULL es complicado; preferimos dejar nullable=True.
        # Si en el futuro migras a Postgres, ahí sí convertís a NOT NULL.

def downgrade():
    # Idempotente: sólo quita la columna si existe
    bind = op.get_bind()
    if _has_column(bind, 'ticket_comentario', 'origen'):
        # Alembic en SQLite no puede drop column fácilmente;
        # dejamos downgrade vacío para evitar romper rollback en dev.
        pass
