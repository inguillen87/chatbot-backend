"""add tenant_id column to chat_session_context"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = 'add_tenant_id_chat_session_context'
down_revision = '21abffd07675'
branch_labels = None
depends_on = None


def _get_connection():
    bind = op.get_bind()
    if hasattr(bind, "execute"):
        return bind
    if hasattr(bind, "connect"):
        return bind.connect()
    raise RuntimeError("No suitable bind/connection available for migration")


def upgrade():
    conn = _get_connection()
    inspector = inspect(conn)

    # Make sure the column exists even if prior runs partially applied changes
    conn.execute(
        text(
            "ALTER TABLE chat_session_context ADD COLUMN IF NOT EXISTS tenant_id INTEGER"
        )
    )

    columns = {col["name"] for col in inspector.get_columns("chat_session_context")}
    if "tenant_id" in columns:
        print("🟢 tenant_id presente en chat_session_context (creado o ya existente)")
    else:
        print("🟡 tenant_id no se detectó tras el intento de creación; revisar manualmente")

    indexes = {idx["name"] for idx in inspector.get_indexes("chat_session_context")}
    if "ix_chat_session_context_tenant_id" not in indexes:
        op.create_index(
            op.f("ix_chat_session_context_tenant_id"),
            "chat_session_context",
            ["tenant_id"],
            unique=False,
        )

    fks = {fk["name"] for fk in inspector.get_foreign_keys("chat_session_context")}
    if "fk_chat_session_context_tenant_id" not in fks:
        op.create_foreign_key(
            "fk_chat_session_context_tenant_id",
            "chat_session_context",
            "tenant_profile",
            ["tenant_id"],
            ["id"],
        )


def downgrade():
    conn = op.get_bind()
    inspector = inspect(conn)

    fks = {fk["name"] for fk in inspector.get_foreign_keys("chat_session_context")}
    if "fk_chat_session_context_tenant_id" in fks:
        op.drop_constraint(
            "fk_chat_session_context_tenant_id", "chat_session_context", type_="foreignkey"
        )

    indexes = {idx["name"] for idx in inspector.get_indexes("chat_session_context")}
    if "ix_chat_session_context_tenant_id" in indexes:
        op.drop_index(op.f("ix_chat_session_context_tenant_id"), table_name="chat_session_context")

    columns = {col["name"] for col in inspector.get_columns("chat_session_context")}
    if "tenant_id" in columns:
        op.drop_column("chat_session_context", "tenant_id")
    print("🔵 tenant_id eliminado de chat_session_context")
