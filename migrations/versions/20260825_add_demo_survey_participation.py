"""add isolated durable receipts for interactive demo survey participation

Revision ID: 20260825_demo_survey_participation_v1
Revises: 20260820_survey_content_jurisdiction_v1
Create Date: 2026-08-25 12:00:00.000000

The deterministic demo instruments are not municipal ``enc_encuesta`` rows.
Their public interactions therefore live in an isolated append-only ledger and
can never be counted as verified citizen responses by the production survey
analytics path.  Each minimized row is both response and exactly-once receipt.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_demo_survey_participation_v1"
down_revision = "20260820_survey_content_jurisdiction_v1"
branch_labels = None
depends_on = None


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION demo_survey_participation_immutable_guard()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'demo survey participation receipts are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_demo_survey_participation_immutable
            BEFORE UPDATE OR DELETE ON demo_survey_participation
            FOR EACH ROW EXECUTE FUNCTION demo_survey_participation_immutable_guard()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_demo_survey_participation_update_immutable
            BEFORE UPDATE ON demo_survey_participation
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'demo survey participation receipts are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_demo_survey_participation_delete_immutable
            BEFORE DELETE ON demo_survey_participation
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'demo survey participation receipts are append-only');
            END
            """
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_demo_survey_participation_immutable "
            "ON demo_survey_participation"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS demo_survey_participation_immutable_guard()"
        )
    elif dialect == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_demo_survey_participation_delete_immutable"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_demo_survey_participation_update_immutable"
        )


def upgrade() -> None:
    op.create_table(
        "demo_survey_participation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("survey_slug", sa.String(length=160), nullable=False),
        sa.Column("tenant_slug", sa.String(length=160), nullable=False),
        sa.Column("sector", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=96), nullable=False),
        sa.Column("option_id", sa.String(length=96), nullable=False),
        sa.Column("submission_id_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("instrument_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "instrument_revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "response_origin",
            sa.String(length=32),
            nullable=False,
            server_default="interactive_demo",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "instrument_revision >= 1",
            name="ck_demo_survey_participation_revision_positive",
        ),
        sa.CheckConstraint(
            "response_origin = 'interactive_demo'",
            name="ck_demo_survey_participation_origin",
        ),
        sa.CheckConstraint(
            "length(submission_id_hash) = 64 AND "
            "length(payload_hash) = 64 AND "
            "length(instrument_sha256) = 64",
            name="ck_demo_survey_participation_hashes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "survey_slug",
            "submission_id_hash",
            name="uq_demo_survey_participation_slug_submission",
        ),
    )
    op.create_index(
        "ix_demo_survey_participation_slug_option",
        "demo_survey_participation",
        ["survey_slug", "instrument_sha256", "option_id"],
        unique=False,
    )
    op.create_index(
        "ix_demo_survey_participation_slug_order",
        "demo_survey_participation",
        ["survey_slug", "instrument_sha256", "id"],
        unique=False,
    )
    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_index(
        "ix_demo_survey_participation_slug_order",
        table_name="demo_survey_participation",
    )
    op.drop_index(
        "ix_demo_survey_participation_slug_option",
        table_name="demo_survey_participation",
    )
    op.drop_table("demo_survey_participation")
