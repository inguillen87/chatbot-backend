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
    / "20260730_add_interview_consent_subject_proof_v3.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "interview_consent_subject_proof_v3", MIGRATION_PATH
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


def _base_schema(connection):
    statements = (
        "CREATE TABLE tenant_profile (id INTEGER PRIMARY KEY)",
        'CREATE TABLE "user" (id INTEGER PRIMARY KEY)',
        "CREATE TABLE chat_session_context (chat_session_id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE channel_session_identity_binding ("
        "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
        "identity_version VARCHAR(32) NOT NULL, identity_hmac VARCHAR(64) NOT NULL, "
        "chat_session_id VARCHAR(36) NOT NULL, UNIQUE (id, tenant_id))",
        "CREATE TABLE assessment_case ("
        "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, source_channel VARCHAR(24) NOT NULL)",
        "CREATE TABLE interview_session ("
        "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, assessment_case_id INTEGER NOT NULL, "
        "channel VARCHAR(24) NOT NULL, UNIQUE (tenant_id, id))",
        "CREATE TABLE whatsapp_outbound_attempt ("
        "id INTEGER PRIMARY KEY, attempt_id VARCHAR(36) NOT NULL UNIQUE)",
        "CREATE TABLE interview_consent_challenge ("
        "id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
        "interview_session_id INTEGER NOT NULL, program_version_id INTEGER NOT NULL, "
        "consent_text_sha256 VARCHAR(64) NOT NULL, "
        "expected_identity_binding_id INTEGER NOT NULL, "
        "expected_identity_hmac VARCHAR(64) NOT NULL, nonce_sha256 VARCHAR(64) NOT NULL, "
        "expires_at DATETIME NOT NULL, issued_by_user_id INTEGER NOT NULL, "
        "issued_at DATETIME NOT NULL, consumed_at DATETIME, consumed_by_user_id INTEGER, "
        "consumed_turn_id INTEGER, consumed_provider_message_sid VARCHAR(180), "
        "contract_version VARCHAR(48) NOT NULL DEFAULT 'interview.consent_challenge.v2')",
    )
    for statement in statements:
        connection.execute(sa.text(statement))


def test_subject_proof_migration_pins_identity_and_creates_immutable_delivery_ledger():
    migration = _load_migration()
    assert migration.revision == "20260730_interview_consent_proof_v3"
    assert migration.down_revision == "20260730_interview_consent_challenge_v2"

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        _base_schema(connection)
        connection.execute(sa.text("INSERT INTO tenant_profile VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" VALUES (1)'))
        connection.execute(
            sa.text("INSERT INTO chat_session_context VALUES ('chat-1'), ('chat-2')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO channel_session_identity_binding VALUES "
                "(20, 1, 'v1', :hmac1, 'chat-1'), "
                "(21, 2, 'v1', :hmac2, 'chat-2')"
            ),
            {"hmac1": "b" * 64, "hmac2": "e" * 64},
        )
        connection.execute(
            sa.text("INSERT INTO assessment_case VALUES (5, 1, 'whatsapp')")
        )
        connection.execute(
            sa.text("INSERT INTO interview_session VALUES (10, 1, 5, 'whatsapp')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO interview_consent_challenge ("
                "id, tenant_id, interview_session_id, program_version_id, "
                "consent_text_sha256, expected_identity_binding_id, expected_identity_hmac, "
                "nonce_sha256, expires_at, issued_by_user_id, issued_at, contract_version) "
                "VALUES (30, 1, 10, 7, :text_hash, 20, :identity_hash, :nonce_hash, "
                "'2026-07-31 00:10:00', 1, '2026-07-31 00:00:00', "
                "'interview.consent_challenge.v2')"
            ),
            {
                "text_hash": "a" * 64,
                "identity_hash": "b" * 64,
                "nonce_hash": "c" * 64,
            },
        )
        connection.execute(
            sa.text("INSERT INTO whatsapp_outbound_attempt VALUES (40, 'attempt-1')")
        )

        _run(migration, connection, "upgrade")
        inspector = sa.inspect(connection)
        assert "interview_consent_presentation" in inspector.get_table_names()
        case = connection.execute(
            sa.text(
                "SELECT subject_identity_binding_id, subject_identity_version, "
                "subject_identity_hmac, subject_chat_session_id FROM assessment_case"
            )
        ).one()
        session = connection.execute(
            sa.text(
                "SELECT subject_identity_binding_id, subject_identity_version, "
                "subject_identity_hmac, subject_chat_session_id FROM interview_session"
            )
        ).one()
        assert tuple(case) == (20, "v1", "b" * 64, "chat-1")
        assert tuple(session) == tuple(case)
        challenge = connection.execute(
            sa.text(
                "SELECT expected_identity_version, expected_chat_session_id, "
                "contract_version, expires_at, issued_at FROM interview_consent_challenge"
            )
        ).one()
        assert challenge[0:3] == (
            "v1",
            "chat-1",
            "interview.consent_challenge.v3",
        )
        assert challenge.expires_at == challenge.issued_at

        insert_presentation = sa.text(
            "INSERT INTO interview_consent_presentation ("
            "tenant_id, interview_session_id, consent_challenge_id, outbound_attempt_id, "
            "outbound_provider, outbound_provider_message_sid, outbound_content_sid, "
            "outbound_provider_status, outbound_provider_status_at, outbound_payload_sha256, "
            "expected_identity_binding_id, expected_identity_version, expected_identity_hmac, "
            "expected_chat_session_id, consent_text_sha256, challenge_nonce_sha256, action, "
            "registered_by_user_id, registration_idempotency_key, registration_request_hash, "
            "registered_at) VALUES ("
            "1, 10, 30, 'attempt-1', 'twilio', 'SM-presented-1', 'HX-consent-v1', "
            ":provider_status, '2026-07-31 00:01:00', :payload_hash, "
            ":binding_id, 'v1', :identity_hash, 'chat-1', :text_hash, :nonce_hash, "
            "'grant_consent', 1, 'presentation:key:001', :request_hash, "
            "'2026-07-31 00:01:01')"
        )
        params = {
            "provider_status": "delivered",
            "payload_hash": "d" * 64,
            "binding_id": 20,
            "identity_hash": "b" * 64,
            "text_hash": "a" * 64,
            "nonce_hash": "c" * 64,
            "request_hash": "f" * 64,
        }
        connection.execute(insert_presentation, params)
        with pytest.raises(IntegrityError):
            connection.execute(insert_presentation, params)
        with pytest.raises(IntegrityError):
            connection.execute(
                insert_presentation,
                {**params, "provider_status": "accepted"},
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    "UPDATE interview_consent_presentation "
                    "SET outbound_provider_status = 'read' WHERE id = 1"
                )
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text("DELETE FROM interview_consent_presentation WHERE id = 1")
            )

        _run(migration, connection, "downgrade")
        inspector = sa.inspect(connection)
        assert "interview_consent_presentation" not in inspector.get_table_names()
        assert "subject_identity_binding_id" not in {
            column["name"] for column in inspector.get_columns("assessment_case")
        }
        assert "expected_identity_version" not in {
            column["name"]
            for column in inspector.get_columns("interview_consent_challenge")
        }


def test_subject_proof_migration_fails_closed_for_unpinned_whatsapp_case():
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        _base_schema(connection)
        connection.execute(sa.text("INSERT INTO tenant_profile VALUES (1)"))
        connection.execute(sa.text('INSERT INTO "user" VALUES (1)'))
        connection.execute(
            sa.text("INSERT INTO assessment_case VALUES (5, 1, 'whatsapp')")
        )
        with pytest.raises(RuntimeError, match="unpinned WhatsApp interview subjects"):
            _run(migration, connection, "upgrade")

