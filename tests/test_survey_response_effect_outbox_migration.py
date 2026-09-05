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
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260728_add_survey_response_effect_outbox.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "survey_response_effect_outbox_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "enc_encuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )
    sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )
    sa.Table(
        "points_transaction",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    return metadata


def _insert_effect(
    connection,
    *,
    effect_id: int,
    tenant_id: int,
    survey_id: int,
    response_id: int,
    effect_type: str,
    effect_key: str,
    scope_key: str,
    status: str | None = None,
    attempt_count: int | None = None,
    max_attempts: int | None = None,
) -> None:
    values = {
        "id": effect_id,
        "tenant_id": tenant_id,
        "survey_id": survey_id,
        "response_id": response_id,
        "effect_type": effect_type,
        "effect_key": effect_key,
        "scope_key": scope_key,
        "payload_json": "{}",
    }
    if status is not None:
        values["status"] = status
    if attempt_count is not None:
        values["attempt_count"] = attempt_count
    if max_attempts is not None:
        values["max_attempts"] = max_attempts

    columns = ", ".join(values)
    parameters = ", ".join(f":{column}" for column in values)
    connection.execute(
        sa.text(
            f"INSERT INTO survey_response_effect_outbox ({columns}) "
            f"VALUES ({parameters})"
        ),
        values,
    )


def test_sqlite_outbox_constraints_defaults_cascade_and_downgrade(tmp_path):
    database_path = tmp_path / "survey-response-effect-outbox.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    _base_metadata().create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "survey_response_effect_outbox" in inspector.get_table_names()

        outbox_columns = {
            column["name"]: column
            for column in inspector.get_columns("survey_response_effect_outbox")
        }
        assert {
            "id",
            "tenant_id",
            "survey_id",
            "response_id",
            "effect_type",
            "effect_key",
            "scope_key",
            "payload_json",
            "status",
            "attempt_count",
            "max_attempts",
            "available_at",
            "lease_token",
            "leased_until",
            "processed_at",
            "result_json",
            "last_error",
            "contract_version",
            "created_at",
            "updated_at",
        } == set(outbox_columns)
        assert "pending" in str(outbox_columns["status"]["default"])
        assert "0" in str(outbox_columns["attempt_count"]["default"])
        assert "8" in str(outbox_columns["max_attempts"]["default"])
        assert "surveys.response_effect.v1" in str(
            outbox_columns["contract_version"]["default"]
        )

        unique_columns = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(
                "survey_response_effect_outbox"
            )
        }
        assert ("effect_key",) in unique_columns
        assert ("response_id", "effect_type") in unique_columns
        assert ("tenant_id", "effect_type", "scope_key") in unique_columns

        foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key
            for foreign_key in inspector.get_foreign_keys(
                "survey_response_effect_outbox"
            )
        }
        assert foreign_keys[("tenant_id",)]["options"].get("ondelete") == "CASCADE"
        assert foreign_keys[("survey_id",)]["options"].get("ondelete") == "CASCADE"
        assert foreign_keys[("response_id",)]["options"].get("ondelete") == "CASCADE"

        outbox_indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("survey_response_effect_outbox")
        }
        assert outbox_indexes["ix_survey_response_effect_due"] == (
            "status",
            "available_at",
            "leased_until",
        )
        assert outbox_indexes["ix_survey_response_effect_tenant_due"] == (
            "tenant_id",
            "status",
            "available_at",
        )

        point_columns = {
            column["name"] for column in inspector.get_columns("points_transaction")
        }
        assert "idempotency_key" in point_columns
        point_indexes = {
            index["name"]: index for index in inspector.get_indexes("points_transaction")
        }
        points_idempotency = point_indexes[
            "uq_points_transaction_tenant_idempotency"
        ]
        assert points_idempotency["unique"] == 1
        assert tuple(points_idempotency["column_names"]) == (
            "tenant_id",
            "idempotency_key",
        )

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(
            sa.text(
                "INSERT INTO enc_encuesta (id, tenant_id) VALUES (10, 1), (20, 2)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta (id, encuesta_id, tenant_id) "
                "VALUES (100, 10, 1), (101, 10, 1), (200, 20, 2)"
            )
        )

        _insert_effect(
            connection,
            effect_id=1,
            tenant_id=1,
            survey_id=10,
            response_id=100,
            effect_type="analytics.v1",
            effect_key="survey-response:100:analytics:v1",
            scope_key="response:100",
        )
        persisted = connection.execute(
            sa.text(
                "SELECT status, attempt_count, max_attempts, contract_version, "
                "available_at, created_at, updated_at "
                "FROM survey_response_effect_outbox WHERE id = 1"
            )
        ).mappings().one()
        assert persisted["status"] == "pending"
        assert persisted["attempt_count"] == 0
        assert persisted["max_attempts"] == 8
        assert persisted["contract_version"] == "surveys.response_effect.v1"
        assert persisted["available_at"] is not None
        assert persisted["created_at"] is not None
        assert persisted["updated_at"] is not None

        duplicate_cases = (
            {
                "effect_id": 2,
                "tenant_id": 2,
                "survey_id": 20,
                "response_id": 200,
                "effect_type": "analytics.v1",
                "effect_key": "survey-response:100:analytics:v1",
                "scope_key": "response:200",
            },
            {
                "effect_id": 3,
                "tenant_id": 1,
                "survey_id": 10,
                "response_id": 100,
                "effect_type": "analytics.v1",
                "effect_key": "survey-response:100:analytics:v1:other",
                "scope_key": "response:100:other",
            },
            {
                "effect_id": 4,
                "tenant_id": 1,
                "survey_id": 10,
                "response_id": 101,
                "effect_type": "analytics.v1",
                "effect_key": "survey-response:101:analytics:v1",
                "scope_key": "response:100",
            },
        )
        for duplicate in duplicate_cases:
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    _insert_effect(connection, **duplicate)

        invalid_cases = (
            {
                "effect_type": "email.v1",
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 8,
            },
            {
                "effect_type": "reward.v1",
                "status": "unknown",
                "attempt_count": 0,
                "max_attempts": 8,
            },
            {
                "effect_type": "reward.v1",
                "status": "pending",
                "attempt_count": -1,
                "max_attempts": 8,
            },
            {
                "effect_type": "reward.v1",
                "status": "pending",
                "attempt_count": 2,
                "max_attempts": 1,
            },
            {
                "effect_type": "reward.v1",
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 0,
            },
        )
        for case_number, invalid in enumerate(invalid_cases, start=10):
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    _insert_effect(
                        connection,
                        effect_id=case_number,
                        tenant_id=1,
                        survey_id=10,
                        response_id=101,
                        effect_key=f"invalid-effect-{case_number}",
                        scope_key=f"invalid-scope-{case_number}",
                        **invalid,
                    )

        connection.execute(
            sa.text(
                "INSERT INTO points_transaction (id, tenant_id, idempotency_key) "
                "VALUES (1, 1, 'survey_reward:10:user:7')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO points_transaction (id, tenant_id, idempotency_key) "
                        "VALUES (2, 1, 'survey_reward:10:user:7')"
                    )
                )
        connection.execute(
            sa.text(
                "INSERT INTO points_transaction (id, tenant_id, idempotency_key) "
                "VALUES (3, 2, 'survey_reward:10:user:7')"
            )
        )

        connection.execute(sa.text("DELETE FROM enc_respuesta WHERE id = 100"))
        assert connection.execute(
            sa.text("SELECT COUNT(*) FROM survey_response_effect_outbox")
        ).scalar_one() == 0

        with Operations.context(context):
            migration.downgrade()

        inspector = sa.inspect(connection)
        assert "survey_response_effect_outbox" not in inspector.get_table_names()
        assert "idempotency_key" not in {
            column["name"]
            for column in inspector.get_columns("points_transaction")
        }

    engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://"])
def test_outbox_migration_compiles_offline_for_sqlite_and_postgresql(url):
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
    assert "ALTER TABLE points_transaction ADD COLUMN idempotency_key" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_points_transaction_tenant_idempotency" in upgrade_sql
    assert "CREATE TABLE survey_response_effect_outbox" in upgrade_sql
    assert "uq_survey_response_effect_response_type" in upgrade_sql
    assert "uq_survey_response_effect_tenant_type_scope" in upgrade_sql
    assert "ON DELETE CASCADE" in upgrade_sql
    assert "CREATE INDEX ix_survey_response_effect_due" in upgrade_sql

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
    assert "DROP TABLE survey_response_effect_outbox" in downgrade_sql
    assert "DROP INDEX uq_points_transaction_tenant_idempotency" in downgrade_sql
    assert "ALTER TABLE points_transaction DROP COLUMN idempotency_key" in downgrade_sql


def test_model_schema_drift_repair_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["20260905_government_launch_v1"]
