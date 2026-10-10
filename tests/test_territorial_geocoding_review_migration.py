from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
QUEUE_MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260830_add_territorial_geocoding_queue_v1.py"
)
REVIEW_MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260830_add_territorial_geocoding_review_v1.py"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_job(connection, *, job_id: str, tenant_id: int, suffix: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO territorial_geocoding_job "
            "(id, tenant_id, contract_version, source_model, source_id, "
            "candidate_fingerprint, address_digest, jurisdiction_digest, status, "
            "reason_code, validation_json, result_json) VALUES "
            "(:id, :tenant_id, 'operations.territorial_geocoding.v1', "
            "'tenant_ticket', :source_id, :fingerprint, :address_digest, "
            ":jurisdiction_digest, 'needs_review', 'provider_partial_match', "
            "'{}', '{}')"
        ),
        {
            "id": job_id,
            "tenant_id": tenant_id,
            "source_id": suffix,
            "fingerprint": suffix * 64,
            "address_digest": ("b" if suffix != "b" else "c") * 64,
            "jurisdiction_digest": ("d" if suffix != "d" else "e") * 64,
        },
    )


def _insert_review(
    connection,
    *,
    review_id: str,
    job_id: str,
    tenant_id: int,
    key_hash: str,
    decision: str = "approved",
    coordinate_write: bool = False,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO territorial_geocoding_review "
            "(id, job_id, tenant_id, reviewer_user_id, contract_version, decision, "
            "reason_code, reviewed_job_status, proposal_digest, "
            "idempotency_key_hash, request_digest, coordinate_write_performed) "
            "VALUES (:id, :job_id, :tenant_id, 10, "
            "'operations.territorial_geocoding_admin.v1', :decision, "
            "'verified_on_map', 'needs_review', :proposal_digest, :key_hash, "
            ":request_digest, :coordinate_write)"
        ),
        {
            "id": review_id,
            "job_id": job_id,
            "tenant_id": tenant_id,
            "decision": decision,
            "proposal_digest": "p" * 64,
            "key_hash": key_hash,
            "request_digest": "r" * 64,
            "coordinate_write": coordinate_write,
        },
    )


def test_review_migration_is_private_idempotent_and_forbids_coordinate_writes(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'territorial-geocoding-review.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    queue_migration = _load(QUEUE_MIGRATION_PATH, "territorial_queue_for_review")
    review_migration = _load(REVIEW_MIGRATION_PATH, "territorial_review_migration")

    assert review_migration.revision == "20260830_geo_review_v1"
    assert review_migration.down_revision == "20260830_territorial_geocoding_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (10)'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            queue_migration.upgrade()
            review_migration.upgrade()

        inspector = sa.inspect(connection)
        assert "territorial_geocoding_review" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns("territorial_geocoding_review")
        }
        assert {
            "reviewer_user_id",
            "decision",
            "reason_code",
            "proposal_digest",
            "idempotency_key_hash",
            "request_digest",
            "coordinate_write_performed",
        }.issubset(columns)
        assert not {
            "address",
            "direccion",
            "domicilio",
            "formatted_address",
            "note",
        }.intersection(columns)

        _insert_job(connection, job_id="job-1", tenant_id=1, suffix="a")
        _insert_review(
            connection,
            review_id="review-1",
            job_id="job-1",
            tenant_id=1,
            key_hash="k" * 64,
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_review(
                    connection,
                    review_id="review-duplicate",
                    job_id="job-1",
                    tenant_id=1,
                    key_hash="k" * 64,
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_review(
                    connection,
                    review_id="review-write",
                    job_id="job-1",
                    tenant_id=1,
                    key_hash="w" * 64,
                    coordinate_write=True,
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_review(
                    connection,
                    review_id="review-invalid-decision",
                    job_id="job-1",
                    tenant_id=1,
                    key_hash="i" * 64,
                    decision="maybe",
                )

        with Operations.context(context):
            review_migration.downgrade()
        assert (
            "territorial_geocoding_review"
            not in sa.inspect(connection).get_table_names()
        )
