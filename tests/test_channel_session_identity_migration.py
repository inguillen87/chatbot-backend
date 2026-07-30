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
    / "20260730_add_channel_session_identity_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "channel_session_identity_migration",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
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


def _bootstrap(connection):
    connection.execute(
        sa.text("CREATE TABLE tenant_profile (id INTEGER PRIMARY KEY)")
    )
    connection.execute(
        sa.text(
            "CREATE TABLE chat_session_context ("
            "chat_session_id VARCHAR(36) PRIMARY KEY, tenant_id INTEGER NULL)"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TABLE whatsapp_inbound_turn ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
            "provider VARCHAR(32) NOT NULL, provider_message_sid VARCHAR(180) NOT NULL, "
            "chat_session_id VARCHAR(64) NULL)"
        )
    )
    connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
    connection.execute(
        sa.text(
            "INSERT INTO chat_session_context (chat_session_id, tenant_id) VALUES "
            "('11111111-1111-1111-1111-111111111111', 1), "
            "('22222222-2222-2222-2222-222222222222', 2)"
        )
    )


def test_identity_migration_revision_and_database_guards():
    migration = _load_migration()
    assert migration.revision == "20260730_channel_session_identity_v1"
    assert migration.down_revision == "20260730_interview_consent_text_v1"

    engine = sa.create_engine("sqlite:///:memory:")
    now = "2026-07-30 06:25:00"
    digest = "a" * 64
    other_digest = "b" * 64
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        _bootstrap(connection)
        _run(migration, connection, "upgrade")

        connection.execute(
            sa.text(
                """
                INSERT INTO channel_session_identity_binding (
                    id, tenant_id, channel, provider, identity_version,
                    identity_hmac, chat_session_id, status, continuity_status,
                    generation, last_verified_at, contract_version, created_at, updated_at
                ) VALUES (
                    10, 1, 'whatsapp', 'twilio', 'v1', :digest,
                    '11111111-1111-1111-1111-111111111111', 'active', 'new',
                    1, :now, 'channel.session_identity.v1', :now, :now
                )
                """
            ),
            {"digest": digest, "now": now},
        )
        # The same citizen HMAC can exist in another tenant, but never twice in
        # the same tenant/channel/provider/version scope.
        connection.execute(
            sa.text(
                """
                INSERT INTO channel_session_identity_binding (
                    id, tenant_id, channel, provider, identity_version,
                    identity_hmac, chat_session_id, status, continuity_status,
                    generation, last_verified_at, contract_version, created_at, updated_at
                ) VALUES (
                    11, 2, 'whatsapp', 'twilio', 'v1', :digest,
                    '22222222-2222-2222-2222-222222222222', 'active', 'new',
                    1, :now, 'channel.session_identity.v1', :now, :now
                )
                """
            ),
            {"digest": digest, "now": now},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO channel_session_identity_binding (
                            id, tenant_id, channel, provider, identity_version,
                            identity_hmac, chat_session_id, status, continuity_status,
                            generation, last_verified_at, contract_version, created_at, updated_at
                        ) VALUES (
                            12, 1, 'whatsapp', 'twilio', 'v1', :digest,
                            '11111111-1111-1111-1111-111111111111', 'active', 'new',
                            1, :now, 'channel.session_identity.v1', :now, :now
                        )
                        """
                    ),
                    {"digest": digest, "now": now},
                )

        connection.execute(
            sa.text(
                """
                INSERT INTO whatsapp_inbound_turn (
                    id, tenant_id, provider, provider_message_sid, chat_session_id,
                    session_identity_binding_id, session_identity_version,
                    session_identity_hmac
                ) VALUES (
                    20, 1, 'twilio', 'SM-safe',
                    '11111111-1111-1111-1111-111111111111', 10, 'v1', :digest
                )
                """
            ),
            {"digest": digest},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO whatsapp_inbound_turn (
                            id, tenant_id, provider, provider_message_sid, chat_session_id,
                            session_identity_binding_id, session_identity_version,
                            session_identity_hmac
                        ) VALUES (
                            21, 2, 'twilio', 'SM-cross-tenant',
                            '11111111-1111-1111-1111-111111111111', 10, 'v1', :digest
                        )
                        """
                    ),
                    {"digest": digest},
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO whatsapp_inbound_turn (
                            id, tenant_id, provider, provider_message_sid, chat_session_id,
                            session_identity_binding_id, session_identity_version,
                            session_identity_hmac
                        ) VALUES (
                            22, 1, 'twilio', 'SM-incomplete',
                            '11111111-1111-1111-1111-111111111111', 10, NULL, :digest
                        )
                        """
                    ),
                    {"digest": other_digest},
                )

        inspector = sa.inspect(connection)
        columns = {
            column["name"]
            for column in inspector.get_columns("whatsapp_inbound_turn")
        }
        assert {
            "session_identity_binding_id",
            "session_identity_version",
            "session_identity_hmac",
        }.issubset(columns)

        _run(migration, connection, "downgrade")
        assert "channel_session_identity_binding" not in set(
            sa.inspect(connection).get_table_names()
        )
