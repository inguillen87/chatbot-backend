"""serialize survey structure edits with the first response

Revision ID: 20260728_survey_guard
Revises: 20260728_claim_receipts
Create Date: 2026-07-28 00:00:03.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_survey_guard"
down_revision = "20260728_claim_receipts"
branch_labels = None
depends_on = None


_TRIGGER_NAME = "trg_enc_respuesta_structure_lock"
_FUNCTION_NAME = "enc_mark_survey_structure_locked"


def _create_response_lock_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE FUNCTION {_FUNCTION_NAME}()
                RETURNS TRIGGER AS $$
                BEGIN
                    UPDATE enc_encuesta
                    SET structure_locked_at = COALESCE(structure_locked_at, CURRENT_TIMESTAMP)
                    WHERE id = NEW.encuesta_id
                      AND structure_locked_at IS NULL;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_TRIGGER_NAME}
                BEFORE INSERT ON enc_respuesta
                FOR EACH ROW
                EXECUTE FUNCTION {_FUNCTION_NAME}()
                """
            )
        )
    elif dialect == "sqlite":
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_TRIGGER_NAME}
                BEFORE INSERT ON enc_respuesta
                FOR EACH ROW
                BEGIN
                    UPDATE enc_encuesta
                    SET structure_locked_at = COALESCE(structure_locked_at, CURRENT_TIMESTAMP)
                    WHERE id = NEW.encuesta_id
                      AND structure_locked_at IS NULL;
                END
                """
            )
        )


def _drop_response_lock_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON enc_respuesta"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()"))
    elif dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}"))


def upgrade() -> None:
    # SQLite supports ADD COLUMN for both definitions.  Direct operations also
    # keep ``alembic --sql`` deploy artifacts deterministic; batch mode would
    # require a live table reflection or a complete ``copy_from`` definition.
    op.add_column(
        "enc_encuesta",
        sa.Column(
            "structure_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "enc_encuesta",
        sa.Column("structure_locked_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Existing participation freezes the instrument forever.  Deleting those
    # rows later deliberately does not clear this marker.
    op.execute(
        sa.text(
            """
            UPDATE enc_encuesta
            SET structure_locked_at = CURRENT_TIMESTAMP
            WHERE structure_locked_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM enc_respuesta
                  WHERE enc_respuesta.encuesta_id = enc_encuesta.id
              )
            """
        )
    )
    _create_response_lock_trigger()


def downgrade() -> None:
    _drop_response_lock_trigger()
    op.drop_column("enc_encuesta", "structure_locked_at")
    op.drop_column("enc_encuesta", "structure_revision")
