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
    / "20260802_add_survey_eligibility_grants_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "survey_eligibility_v1_migration", MIGRATION_PATH
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
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "enc_encuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_enc_encuesta_tenant_id"),
    )
    sa.Table(
        "survey_governance_release",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("eligibility_policy_version", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_survey_governance_release_tenant_id"
        ),
    )
    sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("governance_release_id", sa.Integer(), nullable=True),
        sa.Column(
            "governance_eligibility_policy_version",
            sa.String(64),
            nullable=True,
        ),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TRIGGER trg_enc_respuesta_existing_guard "
                "BEFORE DELETE ON enc_respuesta BEGIN SELECT 1; END"
            )
        )
    return engine


def _upgrade(connection, migration):
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.upgrade()
    return context


def _insert_grant(
    connection,
    *,
    grant_id: int,
    tenant_id: int = 1,
    survey_id: int = 100,
    release_id: int = 1000,
    policy: str = "elig-v1",
    generation: int = 1,
    subject_hmac: str = "b" * 64,
    suffix: str = "a",
    subject_namespace: str = "institution.roster",
    authority_namespace: str = "institution.roster",
    issued_at: str = "2026-08-02 10:00:00",
    expires_at: str = "2099-08-03 10:00:00",
):
    connection.execute(
        sa.text(
            "INSERT INTO survey_eligibility_grant ("
            "id, tenant_id, survey_id, release_id, grant_ref, "
            "eligibility_policy_version, eligibility_mode, subject_namespace, "
            "subject_hmac, generation, "
            "subject_key_version, authority_namespace, authority_adapter_version, "
            "review_reference_hmac, review_key_version, credential_digest, "
            "credential_key_version, issued_by_user_id, issue_idempotency_key, "
            "issue_request_hash, issued_at, expires_at) VALUES ("
            ":id, :tenant, :survey, :release, :grant_ref, :policy, "
            "'institution_attested', :subject_namespace, :subject_hmac, "
            ":generation, 'v1', "
            ":authority_namespace, 'adapter-v1', :review_hmac, 'v1', "
            ":credential_digest, 'v1', 10, :idem, :request_hash, "
            ":issued_at, :expires_at)"
        ),
        {
            "id": grant_id,
            "tenant": tenant_id,
            "survey": survey_id,
            "release": release_id,
            "grant_ref": "seg1_" + suffix * 43,
            "policy": policy,
            "subject_hmac": subject_hmac,
            "subject_namespace": subject_namespace,
            "authority_namespace": authority_namespace,
            "generation": generation,
            "review_hmac": suffix * 64,
            "credential_digest": chr(ord(suffix) + 1) * 64,
            "idem": f"issue-{grant_id:08d}",
            "request_hash": chr(ord(suffix) + 2) * 64,
            "issued_at": issued_at,
            "expires_at": expires_at,
        },
    )


def _seed_scope(connection):
    connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
    connection.execute(sa.text('INSERT INTO "user" (id) VALUES (10), (20)'))
    connection.execute(
        sa.text(
            "INSERT INTO enc_encuesta (id, tenant_id) VALUES (100, 1), (200, 2)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO survey_governance_release "
            "(id, tenant_id, survey_id, eligibility_policy_version) VALUES "
            "(1000, 1, 100, 'elig-v1'), (2000, 2, 200, 'elig-v2')"
        )
    )


def test_migration_enforces_opaque_ledger_scope_and_append_only(tmp_path):
    migration = _load_migration()
    assert migration.revision == "20260802_survey_eligibility_v1"
    assert migration.down_revision == "20260802_interview_assignment_v1"
    engine = _base_engine(tmp_path / "survey-eligibility-populated.sqlite3")

    with engine.begin() as connection:
        _seed_scope(connection)
        context = _upgrade(connection, migration)
        inspector = sa.inspect(connection)
        assert {
            "survey_eligibility_grant",
            "survey_eligibility_terminal",
        }.issubset(inspector.get_table_names())
        response_columns = {
            column["name"] for column in inspector.get_columns("enc_respuesta")
        }
        assert {
            "eligibility_contract_version",
            "eligibility_decision",
            "eligibility_verified_at",
        }.issubset(response_columns)
        trigger_names = {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        assert "trg_enc_respuesta_existing_guard" in trigger_names
        assert "trg_survey_eligibility_terminal_scope" in trigger_names
        assert "trg_enc_respuesta_eligibility_terminal_update" in trigger_names
        assert "trg_survey_eligibility_grant_generation" in trigger_names

        _insert_grant(connection, grant_id=1)
        with pytest.raises(sa.exc.DatabaseError):
            with connection.begin_nested():
                _insert_grant(
                    connection,
                    grant_id=2,
                    generation=3,
                    subject_hmac="b" * 64,
                    suffix="f",
                )
        with pytest.raises(sa.exc.DatabaseError, match="reissue blocked"):
            with connection.begin_nested():
                _insert_grant(
                    connection,
                    grant_id=4,
                    generation=2,
                    subject_hmac="b" * 64,
                    suffix="j",
                )
        with pytest.raises(sa.exc.DatabaseError, match="issued_at is in the future"):
            with connection.begin_nested():
                _insert_grant(
                    connection,
                    grant_id=5,
                    generation=1,
                    subject_hmac="8" * 64,
                    suffix="k",
                    issued_at="2999-01-01 00:00:00",
                    expires_at="2999-01-02 00:00:00",
                )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_grant(
                    connection,
                    grant_id=3,
                    tenant_id=1,
                    survey_id=100,
                    release_id=1000,
                    policy="wrong-policy",
                    subject_hmac="9" * 64,
                    suffix="g",
                )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_grant(
                    connection,
                    grant_id=6,
                    subject_hmac="7" * 64,
                    suffix="l",
                    subject_namespace="school.roster",
                    authority_namespace="institution.roster",
                )

        _insert_grant(
            connection,
            grant_id=7,
            subject_hmac="6" * 64,
            suffix="m",
            issued_at="2026-08-02 10:00:00",
            expires_at="2026-08-02 10:30:00",
        )
        expired_verified_at = "2026-08-02 11:00:00"
        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta ("
                "id, tenant_id, encuesta_id, governance_release_id, "
                "governance_eligibility_policy_version, "
                "eligibility_contract_version, eligibility_decision, "
                "eligibility_verified_at) VALUES (503, 1, 100, 1000, 'elig-v1', "
                "'surveys.public_eligibility.v1', "
                "'verified_by_opaque_grant', :verified_at)"
            ),
            {"verified_at": expired_verified_at},
        )
        with pytest.raises(sa.exc.DatabaseError, match="response scope mismatch"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO survey_eligibility_terminal ("
                        "tenant_id, survey_id, release_id, grant_id, disposition, "
                        "actor_user_id, reason_code, idempotency_key, response_id, "
                        "eligibility_policy_version, submission_payload_hash, "
                        "request_hash, created_at) VALUES ("
                        "1, 100, 1000, 7, 'redeemed', NULL, NULL, NULL, 503, "
                        "'elig-v1', :payload_hash, :request_hash, :created_at)"
                    ),
                    {
                        "payload_hash": "4" * 64,
                        "request_hash": "5" * 64,
                        "created_at": expired_verified_at,
                    },
                )

        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta ("
                "id, tenant_id, encuesta_id, governance_release_id, "
                "governance_eligibility_policy_version) "
                "VALUES (500, 1, 100, 1000, 'elig-v1')"
            )
        )
        with pytest.raises(sa.exc.DatabaseError, match="invalid opaque"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE enc_respuesta SET "
                        "eligibility_decision = 'verified_by_opaque_grant' "
                        "WHERE id = 500"
                    )
                )
        for statement in (
            "INSERT INTO enc_respuesta (id, tenant_id, encuesta_id, "
            "governance_release_id, governance_eligibility_policy_version, "
            "eligibility_contract_version, eligibility_decision, "
            "eligibility_verified_at) VALUES (501, 1, 100, 1000, 'elig-v1', "
            "'surveys.public_eligibility.v1', NULL, CURRENT_TIMESTAMP)",
            "INSERT INTO enc_respuesta (id, tenant_id, encuesta_id, "
            "governance_release_id, governance_eligibility_policy_version, "
            "eligibility_contract_version, eligibility_decision, "
            "eligibility_verified_at) VALUES (502, 1, 100, 1000, 'elig-v1', "
            "NULL, 'verified_by_opaque_grant', CURRENT_TIMESTAMP)",
        ):
            with pytest.raises(sa.exc.DatabaseError, match="invalid opaque"):
                with connection.begin_nested():
                    connection.execute(sa.text(statement))

        verified_at = "2026-08-02 11:00:00"
        connection.execute(
            sa.text(
                "UPDATE enc_respuesta SET "
                "eligibility_contract_version = 'surveys.public_eligibility.v1', "
                "eligibility_decision = 'verified_by_opaque_grant', "
                "eligibility_verified_at = :verified_at WHERE id = 500"
            ),
            {"verified_at": verified_at},
        )

        connection.execute(
            sa.text(
                "INSERT INTO survey_eligibility_terminal ("
                "id, tenant_id, survey_id, release_id, grant_id, disposition, "
                "actor_user_id, reason_code, idempotency_key, response_id, "
                "eligibility_policy_version, submission_payload_hash, request_hash, "
                "created_at) "
                "VALUES (1, 1, 100, 1000, 1, 'redeemed', NULL, NULL, NULL, 500, "
                "'elig-v1', :payload_hash, :request_hash, :created_at)"
            ),
            {
                "payload_hash": "1" * 64,
                "request_hash": "2" * 64,
                "created_at": verified_at,
            },
        )
        with pytest.raises(sa.exc.DatabaseError, match="response receipt is immutable"):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE enc_respuesta SET eligibility_contract_version = NULL, "
                        "eligibility_decision = NULL, eligibility_verified_at = NULL "
                        "WHERE id = 500"
                    )
                )
        for statement in (
            "UPDATE survey_eligibility_grant SET generation = 2 WHERE id = 1",
            "DELETE FROM survey_eligibility_grant WHERE id = 1",
            "UPDATE survey_eligibility_terminal SET disposition = 'revoked' WHERE id = 1",
            "DELETE FROM survey_eligibility_terminal WHERE id = 1",
        ):
            with pytest.raises(sa.exc.DatabaseError, match="history is immutable"):
                with connection.begin_nested():
                    connection.execute(sa.text(statement))

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO survey_eligibility_terminal ("
                        "tenant_id, survey_id, release_id, grant_id, disposition, "
                        "actor_user_id, reason_code, idempotency_key, response_id, "
                        "eligibility_policy_version, submission_payload_hash, request_hash) "
                        "VALUES (1, 100, 1000, 1, 'revoked', 10, "
                        "'administrative_revocation', 'revoke-0001', NULL, "
                        "'elig-v1', NULL, :request_hash)"
                    ),
                    {"request_hash": "3" * 64},
                )

        with pytest.raises(RuntimeError, match="erase audit records"):
            with Operations.context(context):
                migration.downgrade()

    engine.dispose()


def test_migration_rejects_terminal_null_bypass_and_bad_response_scope(tmp_path):
    migration = _load_migration()
    engine = _base_engine(tmp_path / "survey-eligibility-invalid.sqlite3")
    with engine.begin() as connection:
        _seed_scope(connection)
        _upgrade(connection, migration)
        _insert_grant(connection, grant_id=10, subject_hmac="1" * 64, suffix="h")
        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta ("
                "id, tenant_id, encuesta_id, governance_release_id, "
                "governance_eligibility_policy_version) "
                "VALUES (700, 2, 200, 2000, 'elig-v2')"
            )
        )

        invalid_statements = (
            (
                "INSERT INTO survey_eligibility_terminal ("
                "tenant_id, survey_id, release_id, grant_id, disposition, "
                "actor_user_id, reason_code, idempotency_key, response_id, "
                "eligibility_policy_version, submission_payload_hash, request_hash) "
                "VALUES (1, 100, 1000, 10, 'revoked', 10, NULL, "
                "'revoke-null-reason', NULL, 'elig-v1', NULL, :hash)",
                {"hash": "4" * 64},
            ),
            (
                "INSERT INTO survey_eligibility_terminal ("
                "tenant_id, survey_id, release_id, grant_id, disposition, "
                "actor_user_id, reason_code, idempotency_key, response_id, "
                "eligibility_policy_version, submission_payload_hash, request_hash) "
                "VALUES (1, 100, 1000, 10, 'redeemed', NULL, NULL, NULL, 700, "
                "'elig-v1', NULL, :hash)",
                {"hash": "5" * 64},
            ),
            (
                "INSERT INTO survey_eligibility_terminal ("
                "tenant_id, survey_id, release_id, grant_id, disposition, "
                "actor_user_id, reason_code, idempotency_key, response_id, "
                "eligibility_policy_version, submission_payload_hash, request_hash) "
                "VALUES (1, 100, 1000, 10, 'redeemed', NULL, NULL, NULL, 700, "
                "'elig-v1', :payload_hash, :hash)",
                {"payload_hash": "6" * 64, "hash": "7" * 64},
            ),
        )
        for statement, params in invalid_statements:
            with pytest.raises(sa.exc.DatabaseError):
                with connection.begin_nested():
                    connection.execute(sa.text(statement), params)
    engine.dispose()


def test_migration_downgrades_only_empty_ledger_and_preserves_existing_trigger(
    tmp_path,
):
    migration = _load_migration()
    engine = _base_engine(tmp_path / "survey-eligibility-empty.sqlite3")
    with engine.begin() as connection:
        _seed_scope(connection)
        context = _upgrade(connection, migration)
        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta ("
                "id, tenant_id, encuesta_id, governance_release_id, "
                "governance_eligibility_policy_version, "
                "eligibility_contract_version, eligibility_decision, "
                "eligibility_verified_at) VALUES (900, 1, 100, 1000, 'elig-v1', "
                "'surveys.public_eligibility.v1', "
                "'verified_by_opaque_grant', CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(RuntimeError, match="erase audit records"):
            with Operations.context(context):
                migration.downgrade()
        connection.execute(sa.text("DELETE FROM enc_respuesta WHERE id = 900"))
        with Operations.context(context):
            migration.downgrade()
        inspector = sa.inspect(connection)
        assert "survey_eligibility_grant" not in inspector.get_table_names()
        assert "eligibility_decision" not in {
            column["name"] for column in inspector.get_columns("enc_respuesta")
        }
        trigger_names = {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        assert trigger_names == {"trg_enc_respuesta_existing_guard"}
    engine.dispose()
