from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from models import EncRespuesta, PublicSurveyResponse


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260820_add_survey_response_origin.py"
)
HEAD = "20260820_survey_response_origin_v1"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "survey_response_origin_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection, operation) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        operation()


def _legacy_table(metadata: sa.MetaData) -> sa.Table:
    response_table = sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("huella_unica", sa.String(255), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=True),
        sa.UniqueConstraint(
            "encuesta_id",
            "huella_unica",
            name="uq_enc_respuesta_huella",
        ),
    )
    sa.Table(
        "public_survey_response",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
    )
    return response_table


def _trusted_metadata(survey_id: int) -> dict:
    return {
        "is_demo_seed": True,
        "demo_seed_contract_version": "surveys.demo_seeding.v1",
        "demo_batch_id": f"seed-{survey_id}-1234567890-abcdef123456",
    }


def test_sqlite_migration_backfills_only_complete_survey_bound_seed(tmp_path):
    migration = _load_migration()
    assert migration.revision == HEAD
    assert migration.down_revision == "20260815_tenant_reply_event_v1"

    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'survey-origin.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    table = _legacy_table(metadata)
    metadata.create_all(engine)

    rows = [
        {
            "id": 1,
            "encuesta_id": 7,
            "tenant_id": 3,
            "submitted_at": datetime(2026, 8, 20, 10, 0),
            "metadata_payload": json.dumps(_trusted_metadata(7)),
            "huella_unica": "real-or-seed-1",
        },
        {
            "id": 2,
            "encuesta_id": 7,
            "tenant_id": 3,
            "submitted_at": datetime(2026, 8, 20, 10, 1),
            "metadata_payload": json.dumps(
                {**_trusted_metadata(7), "is_demo_seed": 1}
            ),
            "huella_unica": "real-2",
        },
        {
            "id": 3,
            "encuesta_id": 7,
            "tenant_id": 3,
            "submitted_at": datetime(2026, 8, 20, 10, 2),
            "metadata_payload": json.dumps(_trusted_metadata(8)),
            "huella_unica": "real-3",
        },
        {
            "id": 4,
            "encuesta_id": 7,
            "tenant_id": 3,
            "submitted_at": datetime(2026, 8, 20, 10, 3),
            "metadata_payload": json.dumps({"demo": True, "synthetic": True}),
            "huella_unica": "real-4",
        },
        {
            "id": 5,
            "encuesta_id": 7,
            "tenant_id": 3,
            "submitted_at": datetime(2026, 3, 1, 10, 4),
            "metadata_payload": json.dumps(
                {
                    "is_demo_seed": True,
                    "demo_batch_id": "seed-7-1740823440",
                    "demo_scenario": "balanced",
                }
            ),
            "huella_unica": "legacy-5",
        },
        {
            "id": 6,
            "encuesta_id": 7,
            "tenant_id": 3,
            "submitted_at": datetime(2026, 3, 1, 10, 5),
            "metadata_payload": json.dumps(
                {
                    **_trusted_metadata(7),
                    "demo_seed_contract_version": "surveys.demo_seeding.legacy",
                }
            ),
            "huella_unica": "legacy-6",
        },
    ]

    with engine.begin() as connection:
        connection.execute(table.insert(), rows)
        _run(connection, migration.upgrade)

        origins = connection.execute(
            sa.text(
                "SELECT id, response_origin FROM enc_respuesta ORDER BY id"
            )
        ).all()
        assert origins == [
            (1, "synthetic_demo"),
            (2, "real"),
            (3, "real"),
            (4, "real"),
            (5, "legacy_unverified"),
            (6, "legacy_unverified"),
        ]

        inspector = sa.inspect(connection)
        response_origin = {
            column["name"]: column
            for column in inspector.get_columns("enc_respuesta")
        }["response_origin"]
        assert response_origin["nullable"] is False
        indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("enc_respuesta")
        }
        assert indexes["ix_enc_respuesta_survey_origin_submitted_id"] == [
            "encuesta_id",
            "response_origin",
            "submitted_at",
            "id",
        ]
        assert indexes["ix_enc_respuesta_tenant_origin_submitted_id"] == [
            "tenant_id",
            "response_origin",
            "submitted_at",
            "id",
        ]
        assert indexes["uq_enc_respuesta_real_huella"] == [
            "encuesta_id",
            "huella_unica",
        ]
        public_response_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("public_survey_response")
        }
        assert public_response_indexes[
            "ix_public_survey_response_survey_id"
        ] == ["survey_id"]

        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "UPDATE enc_respuesta SET response_origin = 'client_demo' "
                    "WHERE id = 4"
                )
            )

        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta "
                "(id, encuesta_id, tenant_id, huella_unica, submitted_at, "
                "metadata_payload, response_origin) VALUES "
                "(20, 7, 3, 'real-2', '2026-08-20 11:00:00', NULL, "
                "'synthetic_demo'), "
                "(21, 7, 3, 'real-2', '2026-08-20 11:01:00', NULL, "
                "'legacy_unverified')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO enc_respuesta "
                    "(id, encuesta_id, tenant_id, huella_unica, submitted_at, "
                    "metadata_payload, response_origin) VALUES "
                    "(22, 7, 3, 'real-2', '2026-08-20 11:02:00', NULL, 'real')"
                )
            )


def test_model_origin_contract_and_indexes_compile_for_postgresql():
    table_sql = str(
        CreateTable(EncRespuesta.__table__).compile(dialect=postgresql.dialect())
    )
    assert "response_origin" in table_sql
    assert "synthetic_demo" in table_sql
    assert "legacy_unverified" in table_sql
    indexes = {index.name: index for index in EncRespuesta.__table__.indexes}
    for name in {
        "ix_enc_respuesta_survey_origin_submitted_id",
        "ix_enc_respuesta_tenant_origin_submitted_id",
        "uq_enc_respuesta_real_huella",
    }:
        index_sql = str(
            CreateIndex(indexes[name]).compile(dialect=postgresql.dialect())
        )
        assert "response_origin" in index_sql
    real_unique_sql = str(
        CreateIndex(indexes["uq_enc_respuesta_real_huella"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "UNIQUE INDEX" in real_unique_sql
    assert "WHERE response_origin = 'real'" in real_unique_sql
    public_indexes = {
        index.name: index
        for index in PublicSurveyResponse.__table__.indexes
    }
    public_index_sql = str(
        CreateIndex(public_indexes["ix_public_survey_response_survey_id"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "survey_id" in public_index_sql


def test_public_response_index_reuses_exact_shape_and_rejects_name_conflict():
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    table = sa.Table(
        "public_survey_response",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("anon_id", sa.String(80), nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            sa.schema.CreateIndex(
                sa.Index(
                    "ix_public_survey_response_survey_id",
                    table.c.survey_id,
                )
            )
        )
        _run(
            connection,
            lambda: migration._create_index_if_missing_exact(
                "public_survey_response",
                "ix_public_survey_response_survey_id",
                ("survey_id",),
            ),
        )

    conflict_engine = sa.create_engine("sqlite:///:memory:")
    conflict_metadata = sa.MetaData()
    conflict_table = sa.Table(
        "public_survey_response",
        conflict_metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("anon_id", sa.String(80), nullable=True),
    )
    conflict_metadata.create_all(conflict_engine)
    with conflict_engine.begin() as connection:
        connection.execute(
            sa.schema.CreateIndex(
                sa.Index(
                    "ix_public_survey_response_survey_id",
                    conflict_table.c.anon_id,
                )
            )
        )
        with pytest.raises(RuntimeError, match="conflicting index"):
            _run(
                connection,
                lambda: migration._create_index_if_missing_exact(
                    "public_survey_response",
                    "ix_public_survey_response_survey_id",
                    ("survey_id",),
                ),
            )


def test_response_origin_migration_is_forward_only_and_repository_has_one_head():
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="forward-only"):
        migration.downgrade()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]


def test_postgresql_online_migration_contains_low_lock_two_phase_contract():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout = '5s'" in source
    assert "SET LOCAL statement_timeout = '15min'" in source
    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT" in source
    assert source.count("CONCURRENTLY IF NOT EXISTS") >= 4
    assert "autocommit_block" in source

    postgres_upgrade = inspect.getsource(_load_migration()._upgrade_postgresql)
    assert postgres_upgrade.index(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS"
    ) < postgres_upgrade.rindex('"uq_enc_respuesta_huella"')


def test_preflight_rejects_incomplete_legacy_schema_before_mutation():
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        # Deliberately missing huella_unica: an incompatible legacy schema
        # must fail before adding response_origin or rebuilding the table.
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_payload", sa.JSON(), nullable=True),
    )
    sa.Table(
        "public_survey_response",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="missing enc_respuesta columns"):
            _run(connection, migration.upgrade)
        assert "response_origin" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("enc_respuesta")
        }


def test_offline_sql_fails_closed_before_omitting_python_backfill():
    migration = _load_migration()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True},
    )
    with Operations.context(context), pytest.raises(
        RuntimeError,
        match="does not support offline SQL",
    ):
        migration.upgrade()
