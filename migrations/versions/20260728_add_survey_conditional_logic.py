"""add conditional logic to survey questions

Revision ID: 20260728_survey_logic
Revises: 20260728_survey_drafts
Create Date: 2026-07-28 00:00:01.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260728_survey_logic"
down_revision = "20260728_survey_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "enc_pregunta",
        sa.Column("conditional_logic", json_type, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enc_pregunta", "conditional_logic")
