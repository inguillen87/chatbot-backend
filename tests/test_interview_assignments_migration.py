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
    / "20260802_add_interview_assignments_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "interview_assignment_v1_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_engine(path: Path):
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    metadata = sa.MetaData()
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "interview_session",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_interview_session_tenant_id",
        ),
    )
    metadata.create_all(engine)
    return engine


def _insert_assignment(
    connection,
    *,
    tenant_id: int,
    session_id: int,
    version: int,
    idempotency_key: str,
    assignee_user_id: int = 1,
    predecessor_id: int | None = None,
    reason_code: str = "initial_assignment",
    request_hash: str = "a" * 64,
    explicit_id: int | None = None,
):
    connection.execute(
        sa.text(
            "INSERT INTO interview_assignment "
            "(id, tenant_id, interview_session_id, version, assignee_user_id, "
            "previous_assignee_user_id, assigned_by_user_id, reason_code, "
            "supersedes_assignment_id, idempotency_key, request_hash) VALUES "
            "(:id, :tenant_id, :session_id, :version, :assignee_user_id, "
            ":previous_assignee_user_id, "
            ":assigned_by_user_id, :reason_code, :predecessor_id, :idem, :hash)"
        ),
        {
            "id": explicit_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "version": version,
            "assignee_user_id": assignee_user_id,
            "previous_assignee_user_id": assignee_user_id,
            "assigned_by_user_id": assignee_user_id,
            "reason_code": reason_code,
            "predecessor_id": predecessor_id,
            "idem": idempotency_key,
            "hash": request_hash,
        },
    )


def _upgrade(connection, migration):
    connection.execute(sa.text("PRAGMA foreign_keys = ON"))
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.upgrade()
    return context


def test_interview_assignment_migration_is_append_only_and_tenant_scoped(tmp_path):
    migration = _load_migration()
    assert migration.revision == "20260802_interview_assignment_v1"
    assert migration.down_revision == "20260802_campaign_prepare_v1"

    engine = _base_engine(tmp_path / "assignment-populated.sqlite3")
    with engine.begin() as connection:
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1), (2), (3)'))
        connection.execute(
            sa.text(
                "INSERT INTO interview_session (id, tenant_id) VALUES "
                "(10, 1), (11, 1), (20, 2)"
            )
        )
        context = _upgrade(connection, migration)

        inspector = sa.inspect(connection)
        assert "interview_assignment" in inspector.get_table_names()
        assert {
            "id",
            "tenant_id",
            "interview_session_id",
            "version",
            "assignee_user_id",
            "previous_assignee_user_id",
            "assigned_by_user_id",
            "reason_code",
            "supersedes_assignment_id",
            "idempotency_key",
            "request_hash",
            "created_at",
        } == {
            column["name"]
            for column in inspector.get_columns("interview_assignment")
        }
        assert {
            "ix_interview_assignment_tenant_session_version",
            "ix_interview_assignment_tenant_assignee",
        }.issubset(
            {index["name"] for index in inspector.get_indexes("interview_assignment")}
        )
        unique_names = {
            item["name"]
            for item in inspector.get_unique_constraints("interview_assignment")
        }
        assert {
            "uq_interview_assignment_tenant_id",
            "uq_interview_assignment_session_id",
            "uq_interview_assignment_session_version",
            "uq_interview_assignment_tenant_idempotency",
        }.issubset(unique_names)

        _insert_assignment(
            connection,
            tenant_id=1,
            session_id=10,
            version=1,
            idempotency_key="assignment-one",
            explicit_id=100,
        )
        _insert_assignment(
            connection,
            tenant_id=1,
            session_id=10,
            version=2,
            predecessor_id=100,
            reason_code="workload_balance",
            idempotency_key="assignment-two",
            assignee_user_id=2,
            explicit_id=101,
        )

        invalid_rows = [
            {
                "tenant_id": 1,
                "session_id": 10,
                "version": 2,
                "predecessor_id": 100,
                "idempotency_key": "duplicate-version",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 1,
                "idempotency_key": "assignment-one",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 0,
                "idempotency_key": "bad-version",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 1,
                "idempotency_key": "bad-reason",
                "reason_code": "free_text_reason",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 1,
                "idempotency_key": "bad-hash",
                "request_hash": "short",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 2,
                "idempotency_key": "missing-predecessor",
            },
            {
                "tenant_id": 2,
                "session_id": 10,
                "version": 1,
                "idempotency_key": "cross-tenant-session",
            },
            {
                "tenant_id": 2,
                "session_id": 20,
                "version": 2,
                "predecessor_id": 100,
                "idempotency_key": "cross-tenant-predecessor",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 2,
                "predecessor_id": 100,
                "idempotency_key": "cross-session-predecessor",
            },
            {
                "tenant_id": 1,
                "session_id": 11,
                "version": 2,
                "predecessor_id": 110,
                "idempotency_key": "self-predecessor",
                "explicit_id": 110,
            },
        ]
        for invalid in invalid_rows:
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    _insert_assignment(connection, **invalid)

        for statement in (
            "UPDATE interview_assignment SET reason_code = 'availability' WHERE id = 100",
            "DELETE FROM interview_assignment WHERE id = 100",
        ):
            with pytest.raises(sa.exc.DatabaseError, match="history is immutable"):
                with connection.begin_nested():
                    connection.execute(sa.text(statement))

        with pytest.raises(RuntimeError, match="erase audit records"):
            with Operations.context(context):
                migration.downgrade()

    engine.dispose()


def test_interview_assignment_migration_downgrades_only_when_empty(tmp_path):
    migration = _load_migration()
    engine = _base_engine(tmp_path / "assignment-empty.sqlite3")
    with engine.begin() as connection:
        context = _upgrade(connection, migration)
        with Operations.context(context):
            migration.downgrade()
        inspector = sa.inspect(connection)
        assert "interview_assignment" not in inspector.get_table_names()
        trigger_names = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE 'trg_interview_assignment_%'"
                )
            )
        }
        assert trigger_names == set()
    engine.dispose()
