from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260820_survey_content_jurisdiction_v1"
    ]


def test_postgresql_online_migration_contains_low_lock_two_phase_contract():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout = '5s'" in source
    assert "SET LOCAL statement_timeout = '15min'" in source
    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT" in source
    assert source.count("CONCURRENTLY IF NOT EXISTS") >= 4
    assert source.count("DROP INDEX CONCURRENTLY IF EXISTS") >= 4
    assert "autocommit_block" in source

    postgres_upgrade = inspect.getsource(_load_migration()._upgrade_postgresql)
    autocommit_position = postgres_upgrade.index("with context.autocommit_block()")
    drop_position = postgres_upgrade.index("for drop_sql in pending_drop_sql")
    create_position = postgres_upgrade.index("for create_sql in pending_create_sql")
    assert autocommit_position < drop_position < create_position
    post_ddl_validation = postgres_upgrade.index(
        "_assert_postgresql_index_exact_and_valid",
        create_position,
    )
    legacy_constraint_drop = postgres_upgrade.rindex(
        "ALTER TABLE enc_respuesta DROP CONSTRAINT"
    )
    assert create_position < post_ddl_validation < legacy_constraint_drop
    assert postgres_upgrade.index(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS"
    ) < postgres_upgrade.rindex('"uq_enc_respuesta_huella"')


def test_postgresql_invalid_exact_index_plans_concurrent_drop_and_recreate(
    monkeypatch,
):
    migration = _load_migration()
    index_name = "uq_enc_respuesta_real_huella"
    inspector = SimpleNamespace(
        get_indexes=lambda table_name: [
            {
                "name": index_name,
                "column_names": ["encuesta_id", "huella_unica"],
                "unique": True,
                "dialect_options": {
                    "postgresql_where": (
                        "((\"response_origin\")::text = 'real'::text) AND "
                        "(\"huella_unica\" IS NOT NULL)"
                    )
                },
            }
        ]
    )

    class _CatalogBind:
        def __init__(self):
            self.executions = []

        def execute(self, statement, parameters=None):
            self.executions.append(
                (
                    str(statement.compile(dialect=postgresql.dialect())),
                    parameters,
                )
            )
            return SimpleNamespace(
                first=lambda: SimpleNamespace(
                    indisvalid=False,
                    indisready=True,
                    index_definition=(
                        "CREATE UNIQUE INDEX uq_enc_respuesta_real_huella "
                        "ON public.enc_respuesta USING btree "
                        "(encuesta_id, huella_unica) WHERE "
                        "(((response_origin)::text = 'real'::text) AND "
                        "(huella_unica IS NOT NULL))"
                    ),
                )
            )

    bind = _CatalogBind()
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    expected_drop = (
        "DROP INDEX CONCURRENTLY IF EXISTS uq_enc_respuesta_real_huella"
    )
    expected_create = (
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uq_enc_respuesta_real_huella ON enc_respuesta "
        "(encuesta_id, huella_unica) WHERE "
        "response_origin = 'real' AND huella_unica IS NOT NULL"
    )

    planned = migration._plan_postgresql_index_ddl(
        bind,
        table_name="enc_respuesta",
        name=index_name,
        columns=("encuesta_id", "huella_unica"),
        unique=True,
        expected_predicate=(
            "response_origin = 'real' AND huella_unica IS NOT NULL"
        ),
        drop_sql=expected_drop,
        create_sql=expected_create,
    )

    assert planned == (expected_drop, expected_create)
    assert len(bind.executions) == 1
    catalog_sql, catalog_parameters = bind.executions[0]
    assert "FROM pg_index" in catalog_sql
    assert catalog_parameters == {
        "table_name": "enc_respuesta",
        "index_name": index_name,
    }


def test_postgresql_valid_exact_index_plans_no_ddl(monkeypatch):
    migration = _load_migration()
    index_name = "ix_public_survey_response_survey_id"
    inspector = SimpleNamespace(
        get_indexes=lambda table_name: [
            {
                "name": index_name,
                "column_names": ["survey_id"],
                "unique": False,
            }
        ]
    )

    class _CatalogBind:
        def execute(self, statement, parameters=None):
            return SimpleNamespace(
                first=lambda: SimpleNamespace(
                    indisvalid=True,
                    indisready=True,
                    index_definition=(
                        "CREATE INDEX ix_public_survey_response_survey_id "
                        "ON public.public_survey_response USING btree "
                        "(survey_id)"
                    ),
                )
            )

    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    planned = migration._plan_postgresql_index_ddl(
        _CatalogBind(),
        table_name="public_survey_response",
        name=index_name,
        columns=("survey_id",),
        unique=False,
        expected_predicate=None,
        drop_sql=(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_public_survey_response_survey_id"
        ),
        create_sql=(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_public_survey_response_survey_id ON "
            "public_survey_response (survey_id)"
        ),
    )

    assert planned == (None, None)


@pytest.mark.parametrize(
    "conflicting_predicate",
    [
        "response_origin <> 'real' AND huella_unica IS NOT NULL",
        "response_origin = 'REAL' AND huella_unica IS NOT NULL",
        "response_origin = 'real::text' AND huella_unica IS NOT NULL",
    ],
)
def test_postgresql_conflicting_index_predicate_fails_before_repair(
    monkeypatch,
    conflicting_predicate,
):
    migration = _load_migration()
    index_name = "uq_enc_respuesta_real_huella"
    inspector = SimpleNamespace(
        get_indexes=lambda table_name: [
            {
                "name": index_name,
                "column_names": ["encuesta_id", "huella_unica"],
                "unique": True,
                "dialect_options": {
                    "postgresql_where": conflicting_predicate
                },
            }
        ]
    )

    class _ValidCatalogBind:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, statement, parameters=None):
            self.execute_calls += 1
            return SimpleNamespace(
                first=lambda: SimpleNamespace(indisvalid=True, indisready=True)
            )

    bind = _ValidCatalogBind()
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)

    with pytest.raises(RuntimeError, match="conflicting index"):
        migration._plan_postgresql_index_ddl(
            bind,
            table_name="enc_respuesta",
            name=index_name,
            columns=("encuesta_id", "huella_unica"),
            unique=True,
            expected_predicate=(
                "response_origin = 'real' AND huella_unica IS NOT NULL"
            ),
            drop_sql=(
                "DROP INDEX CONCURRENTLY IF EXISTS "
                "uq_enc_respuesta_real_huella"
            ),
            create_sql=(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                "uq_enc_respuesta_real_huella ON enc_respuesta "
                "(encuesta_id, huella_unica) WHERE "
                "response_origin = 'real' AND huella_unica IS NOT NULL"
            ),
        )

    assert bind.execute_calls == 0


@pytest.mark.parametrize(
    "actual_definition",
    [
        (
            "CREATE INDEX ix_enc_respuesta_survey_origin_submitted_id "
            "ON public.enc_respuesta USING hash "
            "(encuesta_id, response_origin, submitted_at, id)"
        ),
        (
            "CREATE INDEX ix_enc_respuesta_survey_origin_submitted_id "
            "ON public.enc_respuesta USING btree "
            "(encuesta_id DESC, response_origin, submitted_at, id)"
        ),
        (
            "CREATE INDEX ix_enc_respuesta_survey_origin_submitted_id "
            "ON public.enc_respuesta USING btree "
            "(encuesta_id, response_origin, submitted_at, id) "
            "INCLUDE (tenant_id)"
        ),
        (
            "CREATE INDEX ix_enc_respuesta_survey_origin_submitted_id "
            "ON public.enc_respuesta USING btree "
            "(encuesta_id int4_ops, response_origin, submitted_at, id)"
        ),
    ],
)
def test_postgresql_valid_noncanonical_definition_fails_closed(
    monkeypatch,
    actual_definition,
):
    migration = _load_migration()
    index_name = "ix_enc_respuesta_survey_origin_submitted_id"
    inspector = SimpleNamespace(
        get_indexes=lambda table_name: [
            {
                "name": index_name,
                "column_names": [
                    "encuesta_id",
                    "response_origin",
                    "submitted_at",
                    "id",
                ],
                "unique": False,
            }
        ]
    )

    class _CatalogBind:
        def execute(self, statement, parameters=None):
            return SimpleNamespace(
                first=lambda: SimpleNamespace(
                    indisvalid=True,
                    indisready=True,
                    index_definition=actual_definition,
                )
            )

    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    expected_create = (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_enc_respuesta_survey_origin_submitted_id ON enc_respuesta "
        "(encuesta_id, response_origin, submitted_at, id)"
    )

    with pytest.raises(RuntimeError, match="unexpected postgresql definition"):
        migration._plan_postgresql_index_ddl(
            _CatalogBind(),
            table_name="enc_respuesta",
            name=index_name,
            columns=("encuesta_id", "response_origin", "submitted_at", "id"),
            unique=False,
            expected_predicate=None,
            drop_sql=(
                "DROP INDEX CONCURRENTLY IF EXISTS "
                "ix_enc_respuesta_survey_origin_submitted_id"
            ),
            create_sql=expected_create,
        )


def test_postgresql_backfill_streaming_select_does_not_contaminate_updates(
    monkeypatch,
):
    migration = _load_migration()

    class _BatchResult:
        def __init__(self):
            self._batches = [
                [
                    SimpleNamespace(
                        id=41,
                        encuesta_id=7,
                        metadata_payload=_trusted_metadata(7),
                    )
                ],
                [],
            ]

        def fetchmany(self, size):
            assert size == 500
            return self._batches.pop(0)

    class _PostgresExecutionSpy:
        """Model SQLAlchemy's in-place connection execution options.

        Render uses one Alembic connection for the streaming SELECT and the
        batched UPDATE.  If the SELECT sets ``stream_results`` on that shared
        connection, Psycopg attempts ``DECLARE ... CURSOR FOR UPDATE``.
        """

        dialect = postgresql.dialect()

        def __init__(self):
            self.connection_options = {}
            self.connection_option_calls = []
            self.executions = []

        def execution_options(self, **options):
            self.connection_option_calls.append(dict(options))
            self.connection_options.update(options)
            return self

        def execute(self, statement, parameters=None):
            statement_options = dict(statement.get_execution_options())
            effective_options = {
                **self.connection_options,
                **statement_options,
            }
            sql = str(statement.compile(dialect=self.dialect))
            execution = {
                "sql": sql,
                "statement_options": statement_options,
                "effective_options": effective_options,
            }
            self.executions.append(execution)
            if statement.is_update and effective_options.get("stream_results"):
                raise AssertionError("DECLARE CURSOR inherited by UPDATE")
            if statement.is_select:
                return _BatchResult()
            return SimpleNamespace()

    bind = _PostgresExecutionSpy()
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    migration._backfill_seed_origins()

    assert bind.connection_option_calls == []
    assert len(bind.executions) == 2
    select_execution, update_execution = bind.executions
    assert select_execution["statement_options"]["stream_results"] is True
    assert select_execution["effective_options"]["stream_results"] is True
    assert update_execution["statement_options"].get("stream_results") is not True
    assert update_execution["effective_options"].get("stream_results") is not True
    assert "UPDATE enc_respuesta" in update_execution["sql"]


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
