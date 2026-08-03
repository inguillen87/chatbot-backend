from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260802_add_whatsapp_workflow_versioning_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "whatsapp_workflow_versioning_v1_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(path: Path):
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    return engine


def _run(connection, callback):
    migration_context = MigrationContext.configure(connection)
    with Operations.context(migration_context):
        callback()


def _insert_draft(
    connection,
    *,
    row_id: str,
    workflow_id: str,
    revision: int,
    digest: str,
    idempotency_key: str,
):
    connection.execute(
        sa.text(
            "INSERT INTO whatsapp_workflow_draft_revision ("
            "id, tenant_id, workflow_id, revision, schema_version, draft_digest, "
            "draft_json, authored_by_user_id, idempotency_key, request_hash) VALUES ("
            ":id, 1, :workflow_id, :revision, 'whatsapp.workflow_draft.v1', :digest, "
            ":draft_json, 10, :idempotency_key, :request_hash)"
        ),
        {
            "id": row_id,
            "workflow_id": workflow_id,
            "revision": revision,
            "digest": digest,
            "draft_json": json.dumps({"name": "Control plane"}),
            "idempotency_key": idempotency_key,
            "request_hash": "f" * 64,
        },
    )


def test_workflow_migration_is_single_head_append_only_and_scope_checked(tmp_path):
    migration = _load_migration()
    assert migration.revision == "20260802_whatsapp_workflow_v1"
    assert migration.down_revision == "20260802_survey_eligibility_v1"
    engine = _engine(tmp_path / "workflow-ledger.sqlite3")

    draft_id = "10000000-0000-4000-8000-000000000001"
    workflow_id = "20000000-0000-4000-8000-000000000001"
    review_id = "30000000-0000-4000-8000-000000000001"
    version_id = "40000000-0000-4000-8000-000000000001"
    activation_id = "50000000-0000-4000-8000-000000000001"
    digest = "a" * 64
    request_hash = "b" * 64
    content_json = json.dumps({"name": "Control plane"})

    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (10), (20)'))
        _run(connection, migration.upgrade)
        inspector = sa.inspect(connection)
        assert {
            "whatsapp_workflow_draft_revision",
            "whatsapp_workflow_review",
            "whatsapp_workflow_version",
            "whatsapp_workflow_activation",
        }.issubset(inspector.get_table_names())
        triggers = {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        assert {
            "trg_wa_workflow_draft_contiguous",
            "trg_wa_workflow_review_subject",
            "trg_wa_workflow_version_review",
            "trg_wa_workflow_activation_ledger",
            "trg_whatsapp_workflow_version_update_immutable",
            "trg_whatsapp_workflow_activation_delete_immutable",
        }.issubset(triggers)

        _insert_draft(
            connection,
            row_id=draft_id,
            workflow_id=workflow_id,
            revision=1,
            digest=digest,
            idempotency_key="draft-migration-0001",
        )
        with pytest.raises(sa.exc.DatabaseError, match="contiguous"):
            with connection.begin_nested():
                _insert_draft(
                    connection,
                    row_id="10000000-0000-4000-8000-000000000003",
                    workflow_id=workflow_id,
                    revision=3,
                    digest="c" * 64,
                    idempotency_key="draft-migration-0003",
                )
        with pytest.raises(sa.exc.DatabaseError, match="subject mismatch"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO whatsapp_workflow_review ("
                        "id, tenant_id, workflow_id, operation, subject_type, subject_id, "
                        "subject_digest, subject_sequence, decision, review_note, reviewed_by_user_id, "
                        "idempotency_key, request_hash) VALUES ("
                        ":id, 2, :workflow_id, 'publish', 'draft_revision', :draft_id, "
                        ":digest, 1, 'approved', 'Reviewed', 20, 'review-wrong-tenant', :hash)"
                    ),
                    {
                        "id": "30000000-0000-4000-8000-000000000099",
                        "workflow_id": workflow_id,
                        "draft_id": draft_id,
                        "digest": digest,
                        "hash": request_hash,
                    },
                )

        author_review_id = "30000000-0000-4000-8000-000000000010"
        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_review ("
                "id, tenant_id, workflow_id, operation, subject_type, subject_id, "
                "subject_digest, subject_sequence, decision, review_note, reviewed_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 'publish', 'draft_revision', :draft_id, :digest, "
                "1, 'approved', 'Author self review', 10, 'review-author-self', :hash)"
            ),
            {
                "id": author_review_id,
                "workflow_id": workflow_id,
                "draft_id": draft_id,
                "digest": digest,
                "hash": request_hash,
            },
        )
        with pytest.raises(sa.exc.DatabaseError, match="independent approval"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO whatsapp_workflow_version ("
                        "id, tenant_id, workflow_id, version, version_kind, "
                        "source_draft_revision_id, restored_from_version_id, review_id, "
                        "schema_version, content_digest, content_json, published_by_user_id, "
                        "idempotency_key, request_hash) VALUES ("
                        "'40000000-0000-4000-8000-000000000097', 1, :workflow_id, 1, "
                        "'publish', :draft_id, NULL, :review_id, 'whatsapp.workflow_draft.v1', "
                        ":digest, :content_json, 20, 'publish-author-reviewed', :hash)"
                    ),
                    {
                        "workflow_id": workflow_id,
                        "draft_id": draft_id,
                        "review_id": author_review_id,
                        "digest": digest,
                        "content_json": content_json,
                        "hash": request_hash,
                    },
                )

        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_review ("
                "id, tenant_id, workflow_id, operation, subject_type, subject_id, "
                "subject_digest, subject_sequence, decision, review_note, reviewed_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 'publish', 'draft_revision', :draft_id, :digest, "
                "2, 'approved', 'Independent approval', 20, 'review-migration-0001', :hash)"
            ),
            {
                "id": review_id,
                "workflow_id": workflow_id,
                "draft_id": draft_id,
                "digest": digest,
                "hash": request_hash,
            },
        )
        with pytest.raises(sa.exc.DatabaseError, match="independent approval"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO whatsapp_workflow_version ("
                        "id, tenant_id, workflow_id, version, version_kind, "
                        "source_draft_revision_id, restored_from_version_id, review_id, "
                        "schema_version, content_digest, content_json, published_by_user_id, "
                        "idempotency_key, request_hash) VALUES ("
                        "'40000000-0000-4000-8000-000000000099', 1, :workflow_id, 1, "
                        "'publish', :draft_id, NULL, :review_id, 'whatsapp.workflow_draft.v1', "
                        ":digest, :content_json, 20, 'publish-self-reviewed', :hash)"
                    ),
                    {
                        "workflow_id": workflow_id,
                        "draft_id": draft_id,
                        "review_id": review_id,
                        "digest": digest,
                        "content_json": content_json,
                        "hash": request_hash,
                    },
                )
        with pytest.raises(sa.exc.DatabaseError, match="published content mismatch"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO whatsapp_workflow_version ("
                        "id, tenant_id, workflow_id, version, version_kind, "
                        "source_draft_revision_id, restored_from_version_id, review_id, "
                        "schema_version, content_digest, content_json, published_by_user_id, "
                        "idempotency_key, request_hash) VALUES ("
                        "'40000000-0000-4000-8000-000000000098', 1, :workflow_id, 1, "
                        "'publish', :draft_id, NULL, :review_id, 'whatsapp.workflow_draft.v1', "
                        ":digest, :content_json, 10, 'publish-wrong-content', :hash)"
                    ),
                    {
                        "workflow_id": workflow_id,
                        "draft_id": draft_id,
                        "review_id": review_id,
                        "digest": digest,
                        "content_json": json.dumps({"name": "Tampered"}),
                        "hash": request_hash,
                    },
                )
        rejection_review_id = "30000000-0000-4000-8000-000000000003"
        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_review ("
                "id, tenant_id, workflow_id, operation, subject_type, subject_id, "
                "subject_digest, subject_sequence, decision, review_note, reviewed_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 'publish', 'draft_revision', :draft_id, :digest, "
                "3, 'rejected', 'Later rejection', 20, 'review-migration-reject', :hash)"
            ),
            {
                "id": rejection_review_id,
                "workflow_id": workflow_id,
                "draft_id": draft_id,
                "digest": digest,
                "hash": "c" * 64,
            },
        )
        with pytest.raises(sa.exc.DatabaseError, match="independent approval"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO whatsapp_workflow_version ("
                        "id, tenant_id, workflow_id, version, version_kind, "
                        "source_draft_revision_id, restored_from_version_id, review_id, "
                        "schema_version, content_digest, content_json, published_by_user_id, "
                        "idempotency_key, request_hash) VALUES ("
                        "'40000000-0000-4000-8000-000000000096', 1, :workflow_id, 1, "
                        "'publish', :draft_id, NULL, :review_id, 'whatsapp.workflow_draft.v1', "
                        ":digest, :content_json, 10, 'publish-superseded', :hash)"
                    ),
                    {
                        "workflow_id": workflow_id,
                        "draft_id": draft_id,
                        "review_id": review_id,
                        "digest": digest,
                        "content_json": content_json,
                        "hash": "d" * 64,
                    },
                )
        latest_review_id = "30000000-0000-4000-8000-000000000004"
        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_review ("
                "id, tenant_id, workflow_id, operation, subject_type, subject_id, "
                "subject_digest, subject_sequence, decision, review_note, reviewed_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 'publish', 'draft_revision', :draft_id, :digest, "
                "4, 'approved', 'Fresh approval', 20, 'review-migration-fresh', :hash)"
            ),
            {
                "id": latest_review_id,
                "workflow_id": workflow_id,
                "draft_id": draft_id,
                "digest": digest,
                "hash": "e" * 64,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_version ("
                "id, tenant_id, workflow_id, version, version_kind, "
                "source_draft_revision_id, restored_from_version_id, review_id, "
                "schema_version, content_digest, content_json, published_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 1, 'publish', :draft_id, NULL, :review_id, "
                "'whatsapp.workflow_draft.v1', :digest, :content_json, 10, "
                "'publish-migration-0001', :hash)"
            ),
            {
                "id": version_id,
                "workflow_id": workflow_id,
                "draft_id": draft_id,
                "review_id": latest_review_id,
                "digest": digest,
                "content_json": content_json,
                "hash": request_hash,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_activation ("
                "id, tenant_id, workflow_id, sequence, workflow_version_id, "
                "previous_activation_id, activation_kind, activated_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 1, :version_id, NULL, 'publish', 10, "
                "'publish-migration-0001', :hash)"
            ),
            {
                "id": activation_id,
                "workflow_id": workflow_id,
                "version_id": version_id,
                "hash": request_hash,
            },
        )
        rollback_review_id = "30000000-0000-4000-8000-000000000005"
        connection.execute(
            sa.text(
                "INSERT INTO whatsapp_workflow_review ("
                "id, tenant_id, workflow_id, operation, subject_type, subject_id, "
                "subject_digest, subject_sequence, decision, review_note, reviewed_by_user_id, "
                "idempotency_key, request_hash) VALUES ("
                ":id, 1, :workflow_id, 'rollback', 'published_version', :version_id, :digest, "
                "1, 'approved', 'Rollback approval', 20, 'review-rollback-duplicate', :hash)"
            ),
            {
                "id": rollback_review_id,
                "workflow_id": workflow_id,
                "version_id": version_id,
                "digest": digest,
                "hash": "6" * 64,
            },
        )
        with pytest.raises(sa.exc.DatabaseError, match="content already active"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO whatsapp_workflow_version ("
                        "id, tenant_id, workflow_id, version, version_kind, "
                        "source_draft_revision_id, restored_from_version_id, review_id, "
                        "schema_version, content_digest, content_json, published_by_user_id, "
                        "idempotency_key, request_hash) VALUES ("
                        "'40000000-0000-4000-8000-000000000002', 1, :workflow_id, 2, "
                        "'rollback', :draft_id, :version_id, :review_id, "
                        "'whatsapp.workflow_draft.v1', :digest, :content_json, 10, "
                        "'rollback-duplicate-digest', :hash)"
                    ),
                    {
                        "workflow_id": workflow_id,
                        "draft_id": draft_id,
                        "version_id": version_id,
                        "review_id": rollback_review_id,
                        "digest": digest,
                        "content_json": content_json,
                        "hash": "7" * 64,
                    },
                )
        with pytest.raises(sa.exc.DatabaseError, match="immutable"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE whatsapp_workflow_version SET content_digest = :digest "
                        "WHERE id = :id"
                    ),
                    {"digest": "d" * 64, "id": version_id},
                )
        with pytest.raises(RuntimeError, match="erase audit records"):
            _run(connection, migration.downgrade)


def test_empty_workflow_migration_can_downgrade(tmp_path):
    migration = _load_migration()
    engine = _engine(tmp_path / "workflow-ledger-empty.sqlite3")

    with engine.begin() as connection:
        _run(connection, migration.upgrade)
        _run(connection, migration.downgrade)
        assert not {
            "whatsapp_workflow_draft_revision",
            "whatsapp_workflow_review",
            "whatsapp_workflow_version",
            "whatsapp_workflow_activation",
        }.intersection(sa.inspect(connection).get_table_names())


def test_postgres_downgrade_locks_before_count_and_drop(monkeypatch):
    migration = _load_migration()
    events = []

    class _Result:
        def scalar_one(self):
            return 0

    class _Bind:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement):
            events.append(("sql", str(statement)))
            return _Result()

    class _Op:
        def __init__(self):
            self.bind = _Bind()

        def get_bind(self):
            return self.bind

        def drop_index(self, name, **_kwargs):
            events.append(("drop_index", name))

        def drop_table(self, name):
            events.append(("drop_table", name))

    monkeypatch.setattr(migration, "op", _Op())
    monkeypatch.setattr(migration, "_is_offline_mode", lambda: False)
    monkeypatch.setattr(
        migration,
        "_drop_guards",
        lambda: events.append(("drop_guards", None)),
    )

    migration.downgrade()

    assert events[0] == ("sql", migration._POSTGRES_DOWNGRADE_LOCK_SQL)
    assert "SELECT COUNT(*) FROM whatsapp_workflow_draft_revision" in events[1][1]
    assert events[2] == ("drop_guards", None)
    assert all(table in events[0][1] for table in migration._TABLES)
