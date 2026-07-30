from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = (
    ROOT / "migrations" / "versions" / "20260730_add_interview_assessment_core_v1.py"
)
CONSENT_PATH = (
    ROOT / "migrations" / "versions" / "20260730_add_interview_consent_text_v1.py"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_interview_consent_snapshot_backfill_guards_and_downgrade(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'interview-consent.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    core = _load(CORE_PATH, "interview_core_for_consent")
    consent = _load(CONSENT_PATH, "interview_consent_text_v1")
    assert consent.revision == "20260730_interview_consent_text_v1"
    assert consent.down_revision == "20260730_ticket_tenant_scope_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1)'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            core.upgrade()

        connection.execute(
            sa.text(
                "INSERT INTO assessment_program "
                "(id, tenant_id, name, program_type, status, created_by_user_id, "
                "idempotency_key, request_hash, created_at, updated_at) VALUES "
                "(1, 1, 'Ingreso', 'school_admission', 'active', 1, "
                "'program:consent01', :hash, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"hash": "1" * 64},
        )
        connection.execute(
            sa.text(
                "INSERT INTO assessment_program_version "
                "(id, tenant_id, program_id, version_number, status, definition_json, "
                "definition_hash, consent_policy_version, consent_text_sha256, "
                "created_by_user_id, create_idempotency_key, create_request_hash, "
                "published_by_user_id, published_at, publish_idempotency_key, "
                "publish_request_hash, created_at) VALUES "
                "(1, 1, 1, 1, 'published', :definition, :definition_hash, 'policy-1', "
                ":consent_hash, 1, 'version:create01', :create_hash, 1, "
                "CURRENT_TIMESTAMP, 'version:publish01', :publish_hash, CURRENT_TIMESTAMP)"
            ),
            {
                "definition": '{"sections":[]}',
                "definition_hash": "2" * 64,
                "consent_hash": "3" * 64,
                "create_hash": "4" * 64,
                "publish_hash": "5" * 64,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE assessment_program SET published_version_number = 1 WHERE id = 1"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO assessment_case "
                "(id, tenant_id, program_id, program_version_id, subject_type, subject_ref, "
                "status, source_channel, created_by_user_id, idempotency_key, request_hash, "
                "created_at, updated_at) VALUES "
                "(1, 1, 1, 1, 'student_applicant', 'student:consentMigration1', "
                "'in_progress', 'web', 1, 'case:consent001', :hash, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"hash": "6" * 64},
        )
        connection.execute(
            sa.text(
                "INSERT INTO interview_session "
                "(id, tenant_id, assessment_case_id, program_version_id, status, channel, "
                "interviewer_user_id, consent_granted, consent_policy_version, "
                "consent_recorded_at, consent_source, create_idempotency_key, "
                "create_request_hash, start_idempotency_key, start_request_hash, "
                "started_at, created_at, updated_at) VALUES "
                "(1, 1, 1, 1, 'active', 'web', 1, 1, 'policy-1', CURRENT_TIMESTAMP, "
                "'web', 'session:create01', :create_hash, 'session:start001', "
                ":start_hash, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"create_hash": "7" * 64, "start_hash": "8" * 64},
        )

        with Operations.context(context):
            consent.upgrade()

        inspector = sa.inspect(connection)
        version_columns = {
            item["name"] for item in inspector.get_columns("assessment_program_version")
        }
        session_columns = {
            item["name"] for item in inspector.get_columns("interview_session")
        }
        assert {
            "consent_text",
            "consent_text_format",
            "consent_text_normalization",
        }.issubset(version_columns)
        assert {
            "consent_text_sha256",
            "consent_attestation_kind",
            "consent_evidence_provider",
            "consent_evidence_ref",
            "consent_evidence_sha256",
            "consent_evidence_captured_at",
            "consent_attested_by_user_id",
        }.issubset(session_columns)
        legacy = connection.execute(
            sa.text(
                "SELECT consent_text_sha256, consent_attestation_kind "
                "FROM interview_session WHERE id = 1"
            )
        ).one()
        assert legacy == ("3" * 64, "legacy_unverified")

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO assessment_program_version "
                        "(id, tenant_id, program_id, version_number, status, definition_json, "
                        "definition_hash, consent_policy_version, consent_text, "
                        "consent_text_sha256, created_by_user_id, create_idempotency_key, "
                        "create_request_hash, created_at) VALUES "
                        "(2, 1, 1, 2, 'draft', :definition, :definition_hash, 'policy-2', "
                        "'Visible text', :consent_hash, 1, 'version:create02', :request_hash, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {
                        "definition": '{"sections":[]}',
                        "definition_hash": "9" * 64,
                        "consent_hash": "a" * 64,
                        "request_hash": "b" * 64,
                    },
                )

        connection.execute(
            sa.text(
                "INSERT INTO interview_session "
                "(id, tenant_id, assessment_case_id, program_version_id, status, channel, "
                "interviewer_user_id, consent_granted, consent_policy_version, "
                "consent_text_sha256, consent_recorded_at, consent_source, "
                "consent_attestation_kind, consent_evidence_provider, "
                "consent_evidence_ref, consent_evidence_sha256, "
                "consent_evidence_captured_at, consent_attested_by_user_id, "
                "create_idempotency_key, create_request_hash, start_idempotency_key, "
                "start_request_hash, started_at, created_at, updated_at) VALUES "
                "(2, 1, 1, 1, 'active', 'web', 1, 1, 'policy-1', :text_hash, "
                "CURRENT_TIMESTAMP, 'web', 'participant_event', 'chatboc', "
                "'web:consentReceiptAbc1', :evidence_hash, CURRENT_TIMESTAMP, 1, "
                "'session:create02', :create_hash, 'session:start002', :start_hash, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "text_hash": "3" * 64,
                "evidence_hash": "c" * 64,
                "create_hash": "d" * 64,
                "start_hash": "e" * 64,
            },
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO interview_session "
                        "(id, tenant_id, assessment_case_id, program_version_id, status, "
                        "channel, interviewer_user_id, consent_granted, "
                        "consent_policy_version, consent_text_sha256, consent_recorded_at, "
                        "consent_source, consent_attestation_kind, consent_evidence_provider, "
                        "consent_evidence_ref, consent_evidence_sha256, "
                        "consent_evidence_captured_at, consent_attested_by_user_id, "
                        "create_idempotency_key, create_request_hash, start_idempotency_key, "
                        "start_request_hash, started_at, created_at, updated_at) VALUES "
                        "(3, 1, 1, 1, 'active', 'web', 1, 1, 'policy-1', :text_hash, "
                        "CURRENT_TIMESTAMP, 'web', 'participant_event', 'chatboc', "
                        "'web:consentReceiptAbc1', :evidence_hash, CURRENT_TIMESTAMP, 1, "
                        "'session:create03', :create_hash, 'session:start003', :start_hash, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "text_hash": "3" * 64,
                        "evidence_hash": "f" * 64,
                        "create_hash": "1" * 64,
                        "start_hash": "2" * 64,
                    },
                )

        with Operations.context(context):
            consent.downgrade()
        downgraded_version_columns = {
            item["name"]
            for item in sa.inspect(connection).get_columns(
                "assessment_program_version"
            )
        }
        downgraded_session_columns = {
            item["name"]
            for item in sa.inspect(connection).get_columns("interview_session")
        }
        assert "consent_text" not in downgraded_version_columns
        assert "consent_text_sha256" not in downgraded_session_columns
        assert "consent_attestation_kind" not in downgraded_session_columns

    engine.dispose()
