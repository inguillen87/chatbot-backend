"""Add prefers_audio to User model (idempotente)"""
from alembic import op
import sqlalchemy as sa

# IDs de Alembic
revision = 'd31a13125cc3'
down_revision = '040692e9390e'  # dejá el que ya tenía este archivo
branch_labels = None
depends_on = None

def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column_name for c in insp.get_columns(table_name))

def upgrade():
    # Si la columna ya existe (porque otra rama la creó), NO la volvemos a crear
    if not _has_column('user', 'prefers_audio'):
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.add_column(sa.Column('prefers_audio', sa.Boolean(), nullable=True))

        # Opcional: setear default a False para filas existentes y luego dejar NOT NULL
        op.execute("UPDATE user SET prefers_audio = 0 WHERE prefers_audio IS NULL")

        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.alter_column('prefers_audio',
                                  existing_type=sa.Boolean(),
                                  nullable=False)

def downgrade():
    # Idempotente: sólo la borramos si existe
    if _has_column('user', 'prefers_audio'):
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.drop_column('prefers_audio')
