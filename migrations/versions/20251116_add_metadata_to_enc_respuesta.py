"""Add metadata column to survey responses.

Revision ID: 20251116_enc_respuesta_metadata
Revises: 20251115_demografia_encuestas
Create Date: 2024-10-16
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251116_enc_respuesta_metadata"
down_revision = "20251115_demografia_encuestas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enc_respuesta", sa.Column("metadata_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("enc_respuesta", "metadata_payload")
