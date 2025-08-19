"""Add LlmInteractionLog table

Revision ID: ce8254522e14
Revises: 071dc98a431a
Create Date: 2025-08-19 21:46:06.065601
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'ce8254522e14'
down_revision = '071dc98a431a'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)

    # ---------- (1) Crear tabla llm_interaction_log si no existe ----------
    tables = set(insp.get_table_names())
    if 'llm_interaction_log' not in tables:
        op.create_table(
            'llm_interaction_log',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('chat_session_id', sa.String(length=36), nullable=False),
            sa.Column('user_query', sa.Text(), nullable=False),
            sa.Column('llm_response_raw', sa.JSON(), nullable=True),
            sa.Column('status', sa.String(length=50), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ['chat_session_id'],
                ['chat_session_context.chat_session_id'],
            ),
            sa.PrimaryKeyConstraint('id'),
        )

    # ---------- (2) Crear índices si faltan (idempotente) ----------
    existing_indexes = {ix['name'] for ix in insp.get_indexes('llm_interaction_log')} if 'llm_interaction_log' in tables else set()
    if 'ix_llm_interaction_log_chat_session_id' not in existing_indexes and 'llm_interaction_log' in (tables | {'llm_interaction_log'}):
        op.create_index(
            'ix_llm_interaction_log_chat_session_id',
            'llm_interaction_log',
            ['chat_session_id'],
            unique=False,
        )
    if 'ix_llm_interaction_log_status' not in existing_indexes and 'llm_interaction_log' in (tables | {'llm_interaction_log'}):
        op.create_index(
            'ix_llm_interaction_log_status',
            'llm_interaction_log',
            ['status'],
            unique=False,
        )

    # ---------- (3) Cambios en user.prefers_audio ----------
    # En Postgres/otros: aplicar alter normal.
    # En SQLite: SALTEAR para evitar DROP/CREATE de 'user' que rompe por FKs.
    if not is_sqlite:
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.alter_column(
                'prefers_audio',
                existing_type=sa.BOOLEAN(),
                nullable=True,
            )
    else:
        # Dejalo como está en SQLite (la app debe tolerar que siga con su nullable actual)
        pass


def downgrade():
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)

    # Revertir cambio en 'user' solo si NO es SQLite
    if not is_sqlite:
        with op.batch_alter_table('user', schema=None) as batch_op:
            batch_op.alter_column(
                'prefers_audio',
                existing_type=sa.BOOLEAN(),
                nullable=False,
            )

    # Borrar índices si existen
    existing_tables = set(insp.get_table_names())
    if 'llm_interaction_log' in existing_tables:
        existing_indexes = {ix['name'] for ix in insp.get_indexes('llm_interaction_log')}
        if 'ix_llm_interaction_log_status' in existing_indexes:
            op.drop_index('ix_llm_interaction_log_status', table_name='llm_interaction_log')
        if 'ix_llm_interaction_log_chat_session_id' in existing_indexes:
            op.drop_index('ix_llm_interaction_log_chat_session_id', table_name='llm_interaction_log')

        # Borrar tabla
        op.drop_table('llm_interaction_log')
