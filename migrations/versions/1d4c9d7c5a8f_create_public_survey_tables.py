"""create tables for public surveys"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "1d4c9d7c5a8f"
down_revision = "3f3b6a9b7d6e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "public_survey",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("titulo", sa.String(length=255), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("municipio_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_public_survey_slug",
        "public_survey",
        ["slug"],
        unique=True,
    )

    op.create_table(
        "public_survey_question",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), sa.ForeignKey("public_survey.id"), nullable=False),
        sa.Column("titulo", sa.String(length=255), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("tipo", sa.String(length=30), nullable=False),
        sa.Column("obligatoria", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "public_survey_option",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("public_survey_question.id"), nullable=False),
        sa.Column("texto", sa.String(length=255), nullable=False),
        sa.Column("valor", sa.String(length=255), nullable=True),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "public_survey_response",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), sa.ForeignKey("public_survey.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("anon_id", sa.String(length=80), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_public_survey_response_anon_id",
        "public_survey_response",
        ["anon_id"],
        unique=False,
    )

    op.create_table(
        "public_survey_answer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("response_id", sa.Integer(), sa.ForeignKey("public_survey_response.id"), nullable=False),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("public_survey_question.id"), nullable=False),
        sa.Column("option_id", sa.Integer(), sa.ForeignKey("public_survey_option.id"), nullable=True),
        sa.Column("valor", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_table("public_survey_answer")
    op.drop_index("ix_public_survey_response_anon_id", table_name="public_survey_response")
    op.drop_table("public_survey_response")
    op.drop_table("public_survey_option")
    op.drop_table("public_survey_question")
    op.drop_index("ix_public_survey_slug", table_name="public_survey")
    op.drop_table("public_survey")
