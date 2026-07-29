from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260728_add_survey_response_receipts.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "survey_response_receipt_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("enc_encuesta", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("enc_respuesta", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    return metadata


def test_sqlite_receipt_constraints_and_cascade(tmp_path):
    database_path = tmp_path / "survey-response-receipt.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    _base_metadata().create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "survey_response_receipt" in inspector.get_table_names()
        unique_columns = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("survey_response_receipt")
        }
        assert ("tenant_id", "submission_id_hash") in unique_columns
        assert ("response_id",) in unique_columns
        foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key
            for foreign_key in inspector.get_foreign_keys("survey_response_receipt")
        }
        assert foreign_keys[("tenant_id",)]["options"].get("ondelete") == "CASCADE"
        assert foreign_keys[("survey_id",)]["options"].get("ondelete") == "CASCADE"
        assert foreign_keys[("response_id",)]["options"].get("ondelete") == "CASCADE"

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1)"))
        connection.execute(sa.text("INSERT INTO enc_encuesta (id) VALUES (10)"))
        connection.execute(sa.text("INSERT INTO enc_respuesta (id) VALUES (100), (101)"))
        connection.execute(
            sa.text(
                "INSERT INTO survey_response_receipt "
                "(id, tenant_id, survey_id, response_id, submission_id_hash, payload_hash, canonical_version, instrument_revision) "
                "VALUES (1, 1, 10, 100, :key_hash, :payload_hash, 'survey-response.v1', 3)"
            ),
            {"key_hash": "a" * 64, "payload_hash": "b" * 64},
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO survey_response_receipt "
                        "(id, tenant_id, survey_id, response_id, submission_id_hash, payload_hash, canonical_version, instrument_revision) "
                        "VALUES (2, 1, 10, 101, :key_hash, :payload_hash, 'survey-response.v1', 3)"
                    ),
                    {"key_hash": "a" * 64, "payload_hash": "c" * 64},
                )

        connection.execute(sa.text("DELETE FROM enc_respuesta WHERE id = 100"))
        assert connection.execute(
            sa.text("SELECT COUNT(*) FROM survey_response_receipt")
        ).scalar_one() == 0

        with Operations.context(context):
            migration.downgrade()
        assert "survey_response_receipt" not in sa.inspect(connection).get_table_names()

    engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://"])
def test_receipt_migration_compiles_offline(url):
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
    assert "CREATE TABLE survey_response_receipt" in upgrade_sql
    assert "uq_survey_response_receipt_tenant_submission" in upgrade_sql

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
    assert "DROP TABLE survey_response_receipt" in downgrade_buffer.getvalue()
