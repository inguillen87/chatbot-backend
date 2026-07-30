from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260730_add_interview_consent_challenge_v2.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "interview_consent_challenge_v2", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module, connection, name):
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original_op = module.op
    module.op = operations
    try:
        getattr(module, name)()
    finally:
        module.op = original_op


def test_challenge_migration_is_post_identity_and_enforces_scope_and_consumption():
    migration = _load_migration()
    assert migration.revision == "20260730_interview_consent_challenge_v2"
    assert migration.down_revision == "20260730_channel_session_identity_v1"

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("CREATE TABLE tenant_profile (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text('CREATE TABLE "user" (id INTEGER PRIMARY KEY)'))
        connection.execute(
            sa.text(
                "CREATE TABLE interview_session (id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, UNIQUE (tenant_id, id))"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE channel_session_identity_binding ("
                "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
                "UNIQUE (id, tenant_id))"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE whatsapp_inbound_turn ("
                "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
                "UNIQUE (id, tenant_id))"
            )
        )
        connection.execute(sa.text("INSERT INTO tenant_profile VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" VALUES (1)'))
        connection.execute(sa.text("INSERT INTO interview_session VALUES (10, 1)"))
        connection.execute(
            sa.text("INSERT INTO channel_session_identity_binding VALUES (20, 1), (21, 2)")
        )
        connection.execute(sa.text("INSERT INTO whatsapp_inbound_turn VALUES (30, 1)"))

        _run(migration, connection, "upgrade")
        insert_sql = sa.text(
            "INSERT INTO interview_consent_challenge ("
            "tenant_id, interview_session_id, program_version_id, "
            "consent_text_sha256, expected_identity_binding_id, "
            "expected_identity_hmac, nonce_sha256, expires_at, "
            "issued_by_user_id, issued_at) VALUES ("
            "1, 10, 7, :text_hash, 20, :identity_hash, :nonce_hash, "
            "'2026-07-31 00:10:00', 1, '2026-07-31 00:00:00')"
        )
        connection.execute(
            insert_sql,
            {
                "text_hash": "a" * 64,
                "identity_hash": "b" * 64,
                "nonce_hash": "c" * 64,
            },
        )

        with pytest.raises(IntegrityError):
            connection.execute(
                insert_sql,
                {
                    "text_hash": "a" * 64,
                    "identity_hash": "b" * 64,
                    "nonce_hash": "c" * 64,
                },
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    "UPDATE interview_consent_challenge SET consumed_at = "
                    "'2026-07-31 00:01:00' WHERE id = 1"
                )
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    "UPDATE interview_consent_challenge SET "
                    "expected_identity_binding_id = 21 WHERE id = 1"
                )
            )

        connection.execute(
            sa.text(
                "UPDATE interview_consent_challenge SET "
                "consumed_at = '2026-07-31 00:01:00', consumed_by_user_id = 1, "
                "consumed_turn_id = 30, consumed_provider_message_sid = 'SMreceipt1' "
                "WHERE id = 1"
            )
        )
        _run(migration, connection, "downgrade")
        assert "interview_consent_challenge" not in sa.inspect(connection).get_table_names()
