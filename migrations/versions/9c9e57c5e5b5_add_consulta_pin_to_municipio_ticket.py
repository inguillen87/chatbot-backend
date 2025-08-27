from alembic import op
import sqlalchemy as sa

# Revisiones
revision = '9c9e57c5e5b5'
down_revision = '8d9127b93083'
branch_labels = None
depends_on = None

def _has_column(bind, table, column):
    insp = sa.inspect(bind)
    return any(c['name'] == column for c in insp.get_columns(table))

def upgrade():
    bind = op.get_bind()

    # 1) Crear columna si no existe
    if not _has_column(bind, 'municipio_ticket', 'consulta_pin'):
        op.add_column(
            'municipio_ticket',
            sa.Column('consulta_pin', sa.String(length=12), nullable=True)
        )

    # 2) Rellenar nulos con un valor por defecto
    op.execute("UPDATE municipio_ticket SET consulta_pin = '000000' WHERE consulta_pin IS NULL")

    # 3) Dejamos la columna nullable en SQLite para evitar recrear tablas
    #    (cuando pasemos a Postgres se hace NOT NULL en otra migration)

def downgrade():
    # En downgrade, simplemente dejamos nullable otra vez si existe
    bind = op.get_bind()
    if _has_column(bind, 'municipio_ticket', 'consulta_pin'):
        op.alter_column('municipio_ticket', 'consulta_pin', nullable=True)
