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
    / "20260730_add_interview_assessment_core_v1.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "interview_assessment_core_v1_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_schema(metadata: sa.MetaData) -> None:
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )


def test_sqlite_interview_core_constraints_immutability_and_downgrade(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'interviews.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _base_schema(metadata)
    metadata.create_all(engine)
    migration = _load_migration_module()
    assert migration.revision == "20260730_interview_core_v1"
    assert migration.down_revision == "20260730_survey_privacy_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1), (2)'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        expected_tables = {
            "assessment_program",
            "assessment_program_version",
            "assessment_case",
            "interview_session",
            "interview_evidence",
        }
        assert expected_tables.issubset(set(inspector.get_table_names()))
        version_columns = {
            column["name"]
            for column in inspector.get_columns("assessment_program_version")
        }
        assert {"create_idempotency_key", "create_request_hash"}.issubset(
            version_columns
        )

        connection.execute(
            sa.text(
                "INSERT INTO assessment_program "
                "(id, tenant_id, name, program_type, status, created_by_user_id, "
                "idempotency_key, request_hash, created_at, updated_at) VALUES "
                "(1, 1, 'Ingreso', 'school_admission', 'draft', 1, "
                "'program:0001', :hash, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"hash": "1" * 64},
        )
        connection.execute(
            sa.text(
                "INSERT INTO assessment_program_version "
                "(id, tenant_id, program_id, version_number, status, "
                "definition_json, definition_hash, consent_policy_version, "
                "consent_text_sha256, created_by_user_id, create_idempotency_key, "
                "create_request_hash, created_at) VALUES "
                "(1, 1, 1, 1, 'draft', :definition, :definition_hash, "
                "'policy-1', :consent_hash, 1, 'version:create-0001', :request_hash, "
                "CURRENT_TIMESTAMP)"
            ),
            {
                "definition": '{"sections":[]}',
                "definition_hash": "2" * 64,
                "consent_hash": "3" * 64,
                "request_hash": "4" * 64,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE assessment_program_version SET status = 'published', "
                "published_by_user_id = 1, published_at = CURRENT_TIMESTAMP, "
                "publish_idempotency_key = 'version:publish-0001', "
                "publish_request_hash = :hash WHERE id = 1"
            ),
            {"hash": "5" * 64},
        )
        connection.execute(
            sa.text(
                "UPDATE assessment_program SET status = 'active', "
                "published_version_number = 1 WHERE id = 1"
            )
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE assessment_program_version "
                        "SET definition_hash = :hash WHERE id = 1"
                    ),
                    {"hash": "9" * 64},
                )

        connection.execute(
            sa.text(
                "INSERT INTO assessment_case "
                "(id, tenant_id, program_id, program_version_id, subject_type, "
                "subject_ref, status, source_channel, created_by_user_id, "
                "idempotency_key, request_hash, created_at, updated_at) VALUES "
                "(1, 1, 1, 1, 'student_applicant', 'student:opaqueAbc123', "
                "'consent_pending', 'web', 1, 'case:00000001', :hash, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"hash": "6" * 64},
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO assessment_case "
                        "(id, tenant_id, program_id, program_version_id, subject_type, "
                        "subject_ref, status, source_channel, created_by_user_id, "
                        "idempotency_key, request_hash, created_at, updated_at) VALUES "
                        "(2, 2, 1, 1, 'student_applicant', 'student:crossTenantAbc', "
                        "'consent_pending', 'web', 2, 'case:00000002', :hash, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"hash": "7" * 64},
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO assessment_case "
                        "(id, tenant_id, program_id, program_version_id, subject_type, "
                        "subject_ref, status, source_channel, created_by_user_id, "
                        "idempotency_key, request_hash, created_at, updated_at) VALUES "
                        "(3, 1, 1, 1, 'student_applicant', 'student:invalidStatusA', "
                        "'admitted', 'web', 1, 'case:00000003', :hash, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"hash": "8" * 64},
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO interview_session "
                        "(id, tenant_id, assessment_case_id, program_version_id, "
                        "status, channel, interviewer_user_id, consent_granted, "
                        "create_idempotency_key, create_request_hash, created_at, "
                        "updated_at) VALUES "
                        "(1, 1, 1, 1, 'active', 'web', 1, 0, "
                        "'session:0000001', :hash, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"hash": "a" * 64},
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO interview_session "
                        "(id, tenant_id, assessment_case_id, program_version_id, "
                        "status, channel, interviewer_user_id, consent_granted, "
                        "consent_policy_version, consent_recorded_at, consent_source, "
                        "create_idempotency_key, create_request_hash, created_at, "
                        "updated_at) VALUES "
                        "(2, 1, 1, 1, 'active', 'web', 1, 1, 'policy-1', "
                        "CURRENT_TIMESTAMP, 'api', 'session:0000002', :hash, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"hash": "b" * 64},
                )

        indexes = {
            index["name"]
            for index in inspector.get_indexes("interview_evidence")
        }
        assert "ix_interview_evidence_session" in indexes

        with Operations.context(context):
            migration.downgrade()
        assert expected_tables.isdisjoint(set(sa.inspect(connection).get_table_names()))

    engine.dispose()
