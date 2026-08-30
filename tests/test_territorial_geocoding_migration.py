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
    / "20260830_add_territorial_geocoding_queue_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "territorial_geocoding_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_job(connection, *, job_id="job-1", tenant_id=1, fingerprint="a" * 64):
    connection.execute(
        sa.text(
            "INSERT INTO territorial_geocoding_job "
            "(id, tenant_id, contract_version, source_model, source_id, "
            "candidate_fingerprint, address_digest, jurisdiction_digest, status, "
            "reason_code, validation_json, result_json) VALUES "
            "(:id, :tenant_id, 'operations.territorial_geocoding.v1', "
            "'municipio_ticket', '419', :fingerprint, :address_digest, "
            ":jurisdiction_digest, 'pending', 'candidate_discovered', '{}', '{}')"
        ),
        {
            "id": job_id,
            "tenant_id": tenant_id,
            "fingerprint": fingerprint,
            "address_digest": "b" * 64,
            "jurisdiction_digest": "c" * 64,
        },
    )


def _insert_attempt(
    connection,
    *,
    attempt_id="attempt-1",
    job_id="job-1",
    tenant_id=1,
    request_digest="d" * 64,
    outcome="pending",
    write_performed=False,
):
    connection.execute(
        sa.text(
            "INSERT INTO territorial_geocoding_attempt "
            "(id, job_id, tenant_id, attempt_number, request_digest, "
            "outcome_status, reason_code, external_call_performed, "
            "write_performed, result_digest, result_json) VALUES "
            "(:id, :job_id, :tenant_id, 1, :request_digest, :outcome, "
            "'dry_run_validated', 1, :write_performed, :result_digest, '{}')"
        ),
        {
            "id": attempt_id,
            "job_id": job_id,
            "tenant_id": tenant_id,
            "request_digest": request_digest,
            "outcome": outcome,
            "write_performed": write_performed,
            "result_digest": "e" * 64,
        },
    )


def test_migration_creates_private_idempotent_queue_and_attempt_receipts(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'territorial-geocoding.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    migration = _load_migration()

    assert migration.revision == "20260830_territorial_geocoding_v1"
    assert migration.down_revision == "20260829_global_writer_authority_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert {
            "territorial_geocoding_job",
            "territorial_geocoding_attempt",
        }.issubset(inspector.get_table_names())
        job_columns = {
            column["name"]
            for column in inspector.get_columns("territorial_geocoding_job")
        }
        attempt_columns = {
            column["name"]
            for column in inspector.get_columns("territorial_geocoding_attempt")
        }
        assert {
            "candidate_fingerprint",
            "address_digest",
            "jurisdiction_digest",
            "status",
            "validation_json",
        }.issubset(job_columns)
        assert {
            "request_digest",
            "external_call_performed",
            "write_performed",
            "result_digest",
        }.issubset(attempt_columns)
        assert not {
            "address",
            "direccion",
            "formatted_address",
            "query_address",
        }.intersection(job_columns | attempt_columns)

        _insert_job(connection)
        _insert_attempt(connection)

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_job(connection, job_id="job-duplicate")

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_attempt(connection, attempt_id="attempt-duplicate")

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_attempt(
                    connection,
                    attempt_id="attempt-invalid-write",
                    request_digest="f" * 64,
                    outcome="pending",
                    write_performed=True,
                )

        _insert_job(
            connection,
            job_id="job-tenant-2",
            tenant_id=2,
            fingerprint="a" * 64,
        )

        with Operations.context(context):
            migration.downgrade()
        assert "territorial_geocoding_job" not in sa.inspect(connection).get_table_names()
