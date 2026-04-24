"""Add municipio_post table for agenda and news entries."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "c4c3d9d3f88b"
down_revision = "b236fbe99d2d"
branch_labels = None
depends_on = None


def _json_type(bind):
    if bind and bind.dialect.name != "sqlite":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    json_type = _json_type(bind)

    op.create_table(
        "municipio_post",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("municipio_id", sa.Integer(), nullable=False),
        sa.Column("tipo_post", sa.String(length=30), nullable=False, server_default="noticia"),
        sa.Column("titulo", sa.String(length=255), nullable=False),
        sa.Column("subtitulo", sa.String(length=255), nullable=True),
        sa.Column("descripcion", sa.Text(), nullable=False),
        sa.Column("tags", json_type, nullable=True),
        sa.Column("imagen_url", sa.String(length=500), nullable=True),
        sa.Column("enlace", sa.String(length=500), nullable=True),
        sa.Column("fecha_evento_inicio", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fecha_evento_fin", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fecha_publicacion", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ubicacion", sa.String(length=255), nullable=True),
        sa.Column("datos_extra", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["municipio_id"], ["user.id"], name="fk_municipio_post_user"),
    )
    op.create_index(
        "ix_municipio_post_municipio_fecha",
        "municipio_post",
        ["municipio_id", "fecha_publicacion"],
    )


def downgrade() -> None:
    op.drop_index("ix_municipio_post_municipio_fecha", table_name="municipio_post")
    op.drop_table("municipio_post")
