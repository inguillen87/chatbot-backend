"""add report_count to enc_comentario

Revision ID: 20260224_add_report_count_to_enc_comentario
Revises: 20300126_add_wine_fields, 63d5d1f8a654
Create Date: 2026-02-24 18:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260224_add_report_count_to_enc_comentario"
down_revision = ("20300126_add_wine_fields", "63d5d1f8a654")
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        columns = inspector.get_columns(table_name)
    except Exception:
        return False
    return any((col.get("name") or "").lower() == column_name.lower() for col in columns)


def upgrade():
    if not _has_column("enc_comentario", "report_count"):
        with op.batch_alter_table("enc_comentario", schema=None) as batch_op:
            batch_op.add_column(sa.Column("report_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    if _has_column("enc_comentario", "report_count"):
        with op.batch_alter_table("enc_comentario", schema=None) as batch_op:
            batch_op.drop_column("report_count")
