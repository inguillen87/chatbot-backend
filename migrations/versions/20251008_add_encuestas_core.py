"""Add core participatory survey tables."""
from alembic import op
import sqlalchemy as sa


revision = "20251008a1b2"
down_revision = "e7d8c6b42f8a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enc_encuesta",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("titulo", sa.String(length=255), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("tipo", sa.String(length=50), nullable=False, server_default="opinion"),
        sa.Column("estado", sa.String(length=30), nullable=False, server_default="borrador"),
        sa.Column("inicio_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fin_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requiere_identidad", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("politica_unicidad", sa.String(length=30), nullable=False, server_default="libre"),
        sa.Column("anonimo_permitido", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("slug", name="uq_enc_encuesta_slug"),
    )

    op.create_table(
        "enc_pregunta",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("orden", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(length=30), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("obligatoria", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("min_selecciones", sa.Integer(), nullable=True),
        sa.Column("max_selecciones", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["encuesta_id"], ["enc_encuesta.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("encuesta_id", "orden", name="uq_enc_pregunta_encuesta_orden"),
    )

    op.create_table(
        "enc_opcion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pregunta_id", sa.Integer(), nullable=False),
        sa.Column("orden", sa.Integer(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("valor", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["pregunta_id"], ["enc_pregunta.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("pregunta_id", "orden", name="uq_enc_opcion_pregunta_orden"),
    )

    op.create_table(
        "enc_respuesta",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("huella_unica", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("dni", sa.String(length=32), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("ua", sa.String(length=255), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("utm_source", sa.String(length=120), nullable=True),
        sa.Column("utm_campaign", sa.String(length=120), nullable=True),
        sa.Column("canal", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["encuesta_id"], ["enc_encuesta.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("encuesta_id", "huella_unica", name="uq_enc_respuesta_huella"),
    )
    op.create_index(
        "ix_enc_respuesta_encuesta_submitted",
        "enc_respuesta",
        ["encuesta_id", "submitted_at"],
        unique=False,
    )
    op.create_index("ix_enc_encuesta_tenant_id", "enc_encuesta", ["tenant_id"], unique=False)
    op.create_index("ix_enc_respuesta_tenant_id", "enc_respuesta", ["tenant_id"], unique=False)

    op.create_table(
        "enc_respuesta_detalle",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("respuesta_id", sa.Integer(), nullable=False),
        sa.Column("pregunta_id", sa.Integer(), nullable=False),
        sa.Column("opcion_id", sa.Integer(), nullable=True),
        sa.Column("texto_libre", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["respuesta_id"], ["enc_respuesta.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pregunta_id"], ["enc_pregunta.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opcion_id"], ["enc_opcion.id"], ondelete="SET NULL"),
    )

    op.create_table(
        "enc_segmento",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("clave", sa.String(length=120), nullable=False),
        sa.Column("valor", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["encuesta_id"], ["enc_encuesta.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("encuesta_id", "clave", "valor", name="uq_enc_segmento_clave_valor"),
    )

    op.create_table(
        "enc_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("slug_publico", sa.String(length=160), nullable=False),
        sa.Column("canal", sa.String(length=64), nullable=True),
        sa.Column("utm_source", sa.String(length=120), nullable=True),
        sa.Column("utm_campaign", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["encuesta_id"], ["enc_encuesta.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("encuesta_id", "slug_publico", name="uq_enc_link_slug"),
    )
    op.create_index("ix_enc_link_slug_publico", "enc_link", ["slug_publico"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_enc_link_slug_publico", table_name="enc_link")
    op.drop_table("enc_link")
    op.drop_table("enc_segmento")
    op.drop_table("enc_respuesta_detalle")
    op.drop_index("ix_enc_respuesta_encuesta_submitted", table_name="enc_respuesta")
    op.drop_index("ix_enc_respuesta_tenant_id", table_name="enc_respuesta")
    op.drop_table("enc_respuesta")
    op.drop_table("enc_opcion")
    op.drop_table("enc_pregunta")
    op.drop_index("ix_enc_encuesta_tenant_id", table_name="enc_encuesta")
    op.drop_table("enc_encuesta")
