from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260730_add_survey_governance_release_v1.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "survey_governance_release_v1_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_schema(metadata: sa.MetaData) -> None:
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
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
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
    )


def _insert_release(connection, *, release_id: int, tenant_id: int, version: int, status: str):
    published = status in {"published", "closed"}
    closed = status == "closed"
    connection.execute(
        sa.text(
            "INSERT INTO survey_governance_release ("
            "id, tenant_id, survey_id, version_number, status, contract_version, "
            "snapshot_json, snapshot_sha256, policy_sha256, "
            "eligibility_policy_version, consent_policy_version, "
            "created_by_user_id, create_idempotency_key, create_request_hash, created_at, "
            "published_by_user_id, published_at, publish_idempotency_key, publish_request_hash, "
            "closed_by_user_id, closed_at, close_idempotency_key, close_request_hash, "
            "closure_manifest_json, closure_manifest_sha256, closed_response_count"
            ") VALUES ("
            ":id, :tenant_id, 10, :version, :status, 'surveys.governance_release.v1', "
            "'{\"instrument\":{}}', :snapshot_hash, :policy_hash, 'elig-v1', 'consent-v1', "
            "1, :create_key, :create_hash, CURRENT_TIMESTAMP, "
            ":published_by, :published_at, :publish_key, :publish_hash, "
            ":closed_by, :closed_at, :close_key, :close_hash, "
            ":manifest, :manifest_hash, :response_count)"
        ),
        {
            "id": release_id,
            "tenant_id": tenant_id,
            "version": version,
            "status": status,
            "snapshot_hash": "a" * 64,
            "policy_hash": "b" * 64,
            "create_key": f"create:key:{tenant_id}:{version}",
            "create_hash": "c" * 64,
            "published_by": 1 if published else None,
            "published_at": "2026-07-30 20:00:00" if published else None,
            "publish_key": f"publish:key:{tenant_id}:{version}" if published else None,
            "publish_hash": "d" * 64 if published else None,
            "closed_by": 1 if closed else None,
            "closed_at": "2026-07-30 21:00:00" if closed else None,
            "close_key": f"close:key:{tenant_id}:{version}" if closed else None,
            "close_hash": "e" * 64 if closed else None,
            "manifest": "{}" if closed else None,
            "manifest_hash": "f" * 64 if closed else None,
            "response_count": 0 if closed else None,
        },
    )


def test_sqlite_governance_release_constraints_triggers_and_downgrade(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'survey-governance.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _base_schema(metadata)
    metadata.create_all(engine)
    migration = _load_migration_module()
    assert migration.revision == "20260730_survey_governance_v1"
    assert migration.down_revision == "20260730_interview_core_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1), (2)'))
        connection.execute(
            sa.text("INSERT INTO enc_encuesta (id, tenant_id) VALUES (10, 1)")
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "survey_governance_release" in inspector.get_table_names()
        response_columns = {
            column["name"] for column in inspector.get_columns("enc_respuesta")
        }
        assert {
            "governance_release_id",
            "governance_eligibility_policy_version",
            "governance_consent_policy_version",
            "governance_acknowledged_at",
        }.issubset(response_columns)

        _insert_release(
            connection, release_id=100, tenant_id=1, version=1, status="published"
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_release(
                    connection,
                    release_id=101,
                    tenant_id=1,
                    version=2,
                    status="published",
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE survey_governance_release "
                        "SET snapshot_sha256 = :hash WHERE id = 100"
                    ),
                    {"hash": "9" * 64},
                )

        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta ("
                "id, tenant_id, encuesta_id, governance_release_id, "
                "governance_eligibility_policy_version, "
                "governance_consent_policy_version, governance_acknowledged_at"
                ") VALUES (1, 1, 10, 100, 'elig-v1', 'consent-v1', CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO enc_respuesta ("
                        "id, tenant_id, encuesta_id, governance_release_id, "
                        "governance_eligibility_policy_version, "
                        "governance_consent_policy_version, governance_acknowledged_at"
                        ") VALUES (2, 2, 10, 100, 'elig-v1', 'consent-v1', CURRENT_TIMESTAMP)"
                    )
                )

        connection.execute(
            sa.text(
                "UPDATE survey_governance_release SET status = 'closed', "
                "closed_by_user_id = 1, closed_at = CURRENT_TIMESTAMP, "
                "close_idempotency_key = 'close:key:1:1', close_request_hash = :hash, "
                "closure_manifest_json = '{}', closure_manifest_sha256 = :manifest_hash, "
                "closed_response_count = 1 WHERE id = 100"
            ),
            {"hash": "e" * 64, "manifest_hash": "f" * 64},
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE survey_governance_release "
                        "SET closed_response_count = 2 WHERE id = 100"
                    )
                )

        with Operations.context(context):
            migration.downgrade()
        assert "survey_governance_release" not in sa.inspect(connection).get_table_names()
        assert "governance_release_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("enc_respuesta")
        }

    engine.dispose()


def test_postgresql_trigger_sql_is_null_safe_and_guards_delete(monkeypatch):
    migration = _load_migration_module()
    statements: list[str] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _FakeOp:
        @staticmethod
        def get_bind():
            return _Bind()

        @staticmethod
        def execute(statement):
            statements.append(str(statement))

    monkeypatch.setattr(migration, "op", _FakeOp())
    migration._create_release_triggers()
    sql = "\n".join(statements)
    assert "IS NOT DISTINCT FROM" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "OLD.status IN ('published', 'closed')" in sql
    assert "NEW.status = 'closed'" in sql
