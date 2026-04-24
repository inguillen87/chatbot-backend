"""Add live voting flags to enc_encuesta

Revision ID: 20280415_add_live_voting_flags_to_enc_encuesta
Revises: 20271201_add_es_empleado_if_missing
Create Date: 2028-04-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20280415_add_live_voting_flags_to_enc_encuesta"
down_revision = "20271201_add_es_empleado_if_missing"
branch_labels = None
depends_on = None


def _get_columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns("enc_encuesta")}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _get_columns(bind)

    if "es_votacion_envivo" not in columns:
        op.add_column(
            "enc_encuesta",
            sa.Column(
                "es_votacion_envivo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.alter_column("enc_encuesta", "es_votacion_envivo", server_default=None)

    if "mostrar_resultados_envivo" not in columns:
        op.add_column(
            "enc_encuesta",
            sa.Column(
                "mostrar_resultados_envivo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.alter_column("enc_encuesta", "mostrar_resultados_envivo", server_default=None)

    if "permitir_comentarios" not in columns:
        op.add_column(
            "enc_encuesta",
            sa.Column(
                "permitir_comentarios",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.alter_column("enc_encuesta", "permitir_comentarios", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    columns = _get_columns(bind)

    if "permitir_comentarios" in columns:
        op.drop_column("enc_encuesta", "permitir_comentarios")
    if "mostrar_resultados_envivo" in columns:
        op.drop_column("enc_encuesta", "mostrar_resultados_envivo")
    if "es_votacion_envivo" in columns:
        op.drop_column("enc_encuesta", "es_votacion_envivo")
