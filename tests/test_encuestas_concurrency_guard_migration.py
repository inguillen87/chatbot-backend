from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260728_add_survey_concurrency_guard.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("survey_guard_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_migration_backfills_irreversible_marker_and_installs_direct_insert_trigger(
    tmp_path,
):
    database_path = tmp_path / "survey-guard-migration.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = sa.MetaData()
    sa.Table(
        "enc_encuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
    )
    metadata.create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO enc_encuesta (id) VALUES (1), (2)"))
        connection.execute(
            sa.text("INSERT INTO enc_respuesta (id, encuesta_id) VALUES (10, 1)")
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        row = connection.execute(
            sa.text(
                "SELECT structure_revision, structure_locked_at "
                "FROM enc_encuesta WHERE id = 1"
            )
        ).one()
        assert row.structure_revision == 1
        assert row.structure_locked_at is not None

        # Removing responses never unlocks a historically answered survey.
        connection.execute(sa.text("DELETE FROM enc_respuesta WHERE encuesta_id = 1"))
        assert connection.execute(
            sa.text("SELECT structure_locked_at FROM enc_encuesta WHERE id = 1")
        ).scalar_one() is not None

        # The database trigger covers direct writers that bypass the service.
        connection.execute(
            sa.text("INSERT INTO enc_respuesta (id, encuesta_id) VALUES (20, 2)")
        )
        assert connection.execute(
            sa.text("SELECT structure_locked_at FROM enc_encuesta WHERE id = 2")
        ).scalar_one() is not None
        trigger_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'trg_enc_respuesta_structure_lock'"
            )
        ).scalar_one()
        assert trigger_count == 1

        with Operations.context(context):
            migration.downgrade()

        columns = {
            row[1]
            for row in connection.execute(sa.text("PRAGMA table_info(enc_encuesta)"))
        }
        assert "structure_revision" not in columns
        assert "structure_locked_at" not in columns
        trigger_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'trg_enc_respuesta_structure_lock'"
            )
        ).scalar_one()
        assert trigger_count == 0

    engine.dispose()


@pytest.mark.parametrize(
    ("url", "upgrade_fragments", "downgrade_fragments"),
    [
        (
            "sqlite://",
            (
                "ALTER TABLE enc_encuesta ADD COLUMN structure_revision",
                "CREATE TRIGGER trg_enc_respuesta_structure_lock",
                "UPDATE enc_encuesta",
            ),
            (
                "DROP TRIGGER IF EXISTS trg_enc_respuesta_structure_lock",
                "ALTER TABLE enc_encuesta DROP COLUMN structure_locked_at",
            ),
        ),
        (
            "postgresql://",
            (
                "ALTER TABLE enc_encuesta ADD COLUMN structure_revision",
                "CREATE FUNCTION enc_mark_survey_structure_locked()",
                "CREATE TRIGGER trg_enc_respuesta_structure_lock",
            ),
            (
                "DROP TRIGGER IF EXISTS trg_enc_respuesta_structure_lock ON enc_respuesta",
                "DROP FUNCTION IF EXISTS enc_mark_survey_structure_locked()",
            ),
        ),
    ],
)
def test_migration_compiles_isolated_offline_sql_for_supported_dialects(
    url,
    upgrade_fragments,
    downgrade_fragments,
):
    migration = _load_migration_module()

    upgrade_buffer = StringIO()
    upgrade_context = MigrationContext.configure(
        url=url,
        opts={
            "as_sql": True,
            "literal_binds": True,
            "output_buffer": upgrade_buffer,
        },
    )
    with Operations.context(upgrade_context):
        migration.upgrade()
    upgrade_sql = upgrade_buffer.getvalue()
    for fragment in upgrade_fragments:
        assert fragment in upgrade_sql

    downgrade_buffer = StringIO()
    downgrade_context = MigrationContext.configure(
        url=url,
        opts={
            "as_sql": True,
            "literal_binds": True,
            "output_buffer": downgrade_buffer,
        },
    )
    with Operations.context(downgrade_context):
        migration.downgrade()
    downgrade_sql = downgrade_buffer.getvalue()
    for fragment in downgrade_fragments:
        assert fragment in downgrade_sql
