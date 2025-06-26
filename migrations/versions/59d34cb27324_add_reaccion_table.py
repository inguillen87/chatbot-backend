"""add reaccion table"""

from alembic import op
import sqlalchemy as sa

revision = "59d34cb27324"
down_revision = "ee3694e2e48a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reaccion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversacion_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("emoji", sa.String(length=5), nullable=False),
        sa.Column("fecha", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["conversacion_id"], ["conversacion.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
    )


def downgrade():
    op.drop_table("reaccion")
