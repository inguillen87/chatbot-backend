"""Add anchor snapshot support for survey responses."""
from alembic import op
import sqlalchemy as sa


revision = "20251008b2c3"
down_revision = "20251008a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enc_anchor_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("algo", sa.String(length=40), nullable=False, server_default="sha256"),
        sa.Column("root_hash", sa.String(length=128), nullable=False),
        sa.Column("total_respuestas", sa.Integer(), nullable=False),
        sa.Column("desde_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hasta_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chain", sa.String(length=40), nullable=True),
        sa.Column("tx_id", sa.String(length=120), nullable=True),
        sa.Column("anchor_status", sa.String(length=30), nullable=False, server_default="draft"),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["encuesta_id"], ["enc_encuesta.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_enc_anchor_snapshot_tenant", "enc_anchor_snapshot", ["tenant_id"], unique=False)

    op.create_foreign_key(
        "fk_enc_respuesta_snapshot",
        "enc_respuesta",
        "enc_anchor_snapshot",
        ["snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_enc_respuesta_snapshot", "enc_respuesta", type_="foreignkey")
    op.drop_index("ix_enc_anchor_snapshot_tenant", table_name="enc_anchor_snapshot")
    op.drop_table("enc_anchor_snapshot")
