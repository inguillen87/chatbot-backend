"""add tenant_id column to chat_session_context"""

from alembic import op
import sqlalchemy as sa

revision = 'add_tenant_id_chat_session_context'
down_revision = '21abffd07675'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    res = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='chat_session_context' AND column_name='tenant_id'
        """
    ).fetchone()

    if not res:
        op.add_column('chat_session_context', sa.Column('tenant_id', sa.String(255), nullable=True))
        print("🟢 tenant_id agregado correctamente en chat_session_context")
    else:
        print("🟡 tenant_id ya existía, no se aplicó ningún cambio")


def downgrade():
    op.drop_column('chat_session_context', 'tenant_id')
    print("🔵 tenant_id eliminado de chat_session_context")
