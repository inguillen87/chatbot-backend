from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "migrations" / "versions"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execution_fencing_migration_round_trips_on_sqlite(tmp_path):
    queue = _load(
        "territorial_queue_v1_for_execution_test",
        "20260830_add_territorial_geocoding_queue_v1.py",
    )
    review = _load(
        "territorial_review_v1_for_execution_test",
        "20260830_add_territorial_geocoding_review_v1.py",
    )
    execution = _load(
        "territorial_execution_v2_test",
        "20260904_harden_territorial_geocoding_execution_v2.py",
    )
    assert execution.revision == "20260904_geo_execution_v2"
    assert execution.down_revision == "20260904_tenant_reply_delivery_v1"

    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'execution.sqlite3').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE tenant_profile (id INTEGER PRIMARY KEY)")
            )
            connection.execute(sa.text("CREATE TABLE user (id INTEGER PRIMARY KEY)"))
            for migration in (queue, review, execution):
                migration.op = Operations(MigrationContext.configure(connection))
                migration.upgrade()

            inspector = sa.inspect(connection)
            attempt_columns = {
                column["name"]
                for column in inspector.get_columns("territorial_geocoding_attempt")
            }
            review_columns = {
                column["name"]
                for column in inspector.get_columns("territorial_geocoding_review")
            }
            assert {"action", "idempotency_key_hash"} <= attempt_columns
            assert {"proposal_attempt_id", "proposal_attempt_number"} <= review_columns
            assert {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(
                    "territorial_geocoding_attempt"
                )
            } >= {"uq_territorial_geocoding_attempt_idempotency"}

            execution.op = Operations(MigrationContext.configure(connection))
            execution.downgrade()
            downgraded_attempt_columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns(
                    "territorial_geocoding_attempt"
                )
            }
            assert "action" not in downgraded_attempt_columns
            assert "idempotency_key_hash" not in downgraded_attempt_columns
    finally:
        engine.dispose()
