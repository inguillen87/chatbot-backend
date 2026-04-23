"""add puntos_recompensa to enc_encuesta"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20251130_add_puntos_recompensa_to_enc_encuesta"
down_revision = "add_tenant_id_chat_session_context"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)

    columns = {col["name"] for col in inspector.get_columns("enc_encuesta")}
    if "puntos_recompensa" not in columns:
        op.add_column(
            "enc_encuesta",
            sa.Column(
                "puntos_recompensa",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.alter_column(
            "enc_encuesta",
            "puntos_recompensa",
            server_default=None,
        )
    else:
        print("🟡 puntos_recompensa ya existe en enc_encuesta; no se aplicaron cambios")


def downgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("enc_encuesta")}
    if "puntos_recompensa" in columns:
        op.drop_column("enc_encuesta", "puntos_recompensa")
        print("🔵 puntos_recompensa eliminado de enc_encuesta")
    else:
        print("🟡 puntos_recompensa no estaba presente; sin cambios")
