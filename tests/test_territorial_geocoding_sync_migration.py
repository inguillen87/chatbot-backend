from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
SYNC_MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260830_add_territorial_geocoding_sync_receipt_v1.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "territorial_geocoding_sync_migration", SYNC_MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_receipt(
    connection,
    *,
    receipt_id: str,
    tenant_id: int,
    key_hash: str,
    request_digest: str = "r" * 64,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO territorial_geocoding_sync_receipt "
            "(id, tenant_id, actor_user_id, contract_version, "
            "idempotency_key_hash, request_digest, completed, response_json, "
            "created_at, updated_at) VALUES "
            "(:id, :tenant_id, 10, 'operations.territorial_geocoding_sync.v1', "
            ":key_hash, :request_digest, 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "id": receipt_id,
            "tenant_id": tenant_id,
            "key_hash": key_hash,
            "request_digest": request_digest,
        },
    )


def test_sync_receipt_migration_is_tenant_scoped_private_and_idempotent(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'territorial-geocoding-sync.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    migration = _load()

    assert migration.revision == "20260830_geo_sync_v1"
    assert migration.down_revision == "20260830_geo_review_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (10)'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "territorial_geocoding_sync_receipt" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns(
                "territorial_geocoding_sync_receipt"
            )
        }
        assert {
            "tenant_id",
            "actor_user_id",
            "idempotency_key_hash",
            "request_digest",
            "completed",
            "response_json",
        }.issubset(columns)
        assert not {
            "address",
            "direccion",
            "domicilio",
            "coordinates",
            "lat",
            "lng",
            "idempotency_key",
        }.intersection(columns)

        _insert_receipt(
            connection,
            receipt_id="receipt-1",
            tenant_id=1,
            key_hash="k" * 64,
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    receipt_id="receipt-duplicate",
                    tenant_id=1,
                    key_hash="k" * 64,
                )

        # The same opaque key hash is isolated by tenant.
        _insert_receipt(
            connection,
            receipt_id="receipt-tenant-2",
            tenant_id=2,
            key_hash="k" * 64,
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    receipt_id="receipt-bad-digest",
                    tenant_id=1,
                    key_hash="short",
                )

        with Operations.context(context):
            migration.downgrade()
        assert (
            "territorial_geocoding_sync_receipt"
            not in sa.inspect(connection).get_table_names()
        )
