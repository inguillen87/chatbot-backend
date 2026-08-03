from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "versions" / "20260728_add_survey_materialization.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("survey_materialization_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "survey_draft",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.String(160), nullable=False),
    )
    sa.Table("enc_encuesta", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "enc_pregunta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
    )
    sa.Table(
        "enc_opcion",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pregunta_id", sa.Integer(), nullable=False),
    )
    return metadata


def test_sqlite_upgrade_and_downgrade_create_receipts_refs_and_restrict_fks(tmp_path):
    database_path = tmp_path / "survey-materialization.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    _base_metadata().create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "document_ref" in {column["name"] for column in inspector.get_columns("enc_encuesta")}
        assert "logical_ref" in {column["name"] for column in inspector.get_columns("enc_pregunta")}
        assert "logical_ref" in {column["name"] for column in inspector.get_columns("enc_opcion")}
        assert "survey_draft_materialization" in inspector.get_table_names()
        assert "survey_draft_materialization_alias" in inspector.get_table_names()

        question_indexes = {index["name"]: index for index in inspector.get_indexes("enc_pregunta")}
        option_indexes = {index["name"]: index for index in inspector.get_indexes("enc_opcion")}
        assert question_indexes["uq_enc_pregunta_encuesta_logical_ref"]["unique"] == 1
        assert option_indexes["uq_enc_opcion_pregunta_logical_ref"]["unique"] == 1

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1)'))
        connection.execute(
            sa.text(
                "INSERT INTO survey_draft (id, tenant_id, draft_id) "
                "VALUES (1, 1, 'draft-migration')"
            )
        )
        connection.execute(sa.text("INSERT INTO enc_encuesta (id, document_ref) VALUES (1, 'draft-migration')"))
        connection.execute(
            sa.text(
                "INSERT INTO survey_draft_materialization "
                "(id, tenant_id, survey_draft_id, survey_id, draft_id, draft_revision, "
                "schema_version, document_ref, payload_hash, operation_fingerprint, "
                "idempotency_key, created_by) VALUES "
                "(1, 1, 1, 1, 'draft-migration', 1, 'survey-document.v1', "
                "'draft-migration', :hash, :fingerprint, 'materialize-migration', 1)"
            ),
            {"hash": "a" * 64, "fingerprint": "b" * 64},
        )
        connection.execute(
            sa.text(
                "INSERT INTO survey_draft_materialization_alias "
                "(id, tenant_id, materialization_id, idempotency_key, operation_fingerprint) "
                "VALUES (1, 1, 1, 'materialize-migration', :fingerprint)"
            ),
            {"fingerprint": "b" * 64},
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(sa.text("DELETE FROM enc_encuesta WHERE id = 1"))

        connection.execute(sa.text("DELETE FROM survey_draft_materialization_alias"))
        connection.execute(sa.text("DELETE FROM survey_draft_materialization"))
        with Operations.context(context):
            migration.downgrade()

        inspector = sa.inspect(connection)
        assert "survey_draft_materialization" not in inspector.get_table_names()
        assert "survey_draft_materialization_alias" not in inspector.get_table_names()
        assert "document_ref" not in {
            column["name"] for column in inspector.get_columns("enc_encuesta")
        }
        assert "logical_ref" not in {
            column["name"] for column in inspector.get_columns("enc_pregunta")
        }
        assert "logical_ref" not in {
            column["name"] for column in inspector.get_columns("enc_opcion")
        }

    engine.dispose()


@pytest.mark.parametrize(
    ("url", "upgrade_fragments", "downgrade_fragments"),
    [
        (
            "sqlite://",
            (
                "ALTER TABLE enc_encuesta ADD COLUMN document_ref",
                "CREATE UNIQUE INDEX uq_enc_pregunta_encuesta_logical_ref",
                "CREATE TABLE survey_draft_materialization",
                "CREATE TABLE survey_draft_materialization_alias",
            ),
            (
                "DROP TABLE survey_draft_materialization_alias",
                "ALTER TABLE enc_encuesta DROP COLUMN document_ref",
            ),
        ),
        (
            "postgresql://",
            (
                "ALTER TABLE enc_encuesta ADD COLUMN document_ref",
                "CREATE UNIQUE INDEX uq_enc_opcion_pregunta_logical_ref",
                "CREATE TABLE survey_draft_materialization",
                "FOREIGN KEY(survey_id) REFERENCES enc_encuesta (id) ON DELETE RESTRICT",
            ),
            (
                "DROP TABLE survey_draft_materialization",
                "ALTER TABLE enc_pregunta DROP COLUMN logical_ref",
            ),
        ),
    ],
)
def test_migration_compiles_offline_for_sqlite_and_postgresql(
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


def test_domain_effect_outbox_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["20260802_whatsapp_workflow_v1"]
