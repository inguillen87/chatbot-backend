"""Add demographic fields to survey responses.

Revision ID: 20251115_add_demografia_encuestas
Revises: 20251010_merge_encuestas_features_heads
Create Date: 2024-05-21
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251115_add_demografia_encuestas"
down_revision = "20251010_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enc_respuesta", sa.Column("genero", sa.String(length=30), nullable=True))
    op.add_column("enc_respuesta", sa.Column("edad", sa.Integer(), nullable=True))
    op.add_column("enc_respuesta", sa.Column("anio_nacimiento", sa.Integer(), nullable=True))
    op.add_column("enc_respuesta", sa.Column("rango_etario", sa.String(length=30), nullable=True))
    op.add_column("enc_respuesta", sa.Column("barrio", sa.String(length=120), nullable=True))
    op.add_column("enc_respuesta", sa.Column("ciudad", sa.String(length=120), nullable=True))
    op.add_column("enc_respuesta", sa.Column("provincia", sa.String(length=120), nullable=True))
    op.add_column("enc_respuesta", sa.Column("pais", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("enc_respuesta", "pais")
    op.drop_column("enc_respuesta", "provincia")
    op.drop_column("enc_respuesta", "ciudad")
    op.drop_column("enc_respuesta", "barrio")
    op.drop_column("enc_respuesta", "rango_etario")
    op.drop_column("enc_respuesta", "anio_nacimiento")
    op.drop_column("enc_respuesta", "edad")
    op.drop_column("enc_respuesta", "genero")
