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
    / "20260905_add_tenant_blueprint_application_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "tenant_blueprint_application_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_receipt(
    connection,
    *,
    receipt_id: str,
    tenant_id: int = 1,
    idempotency_hash: str = "a" * 64,
    version: str = "1.0.0",
):
    connection.execute(
        sa.text(
            "INSERT INTO tenant_blueprint_application "
            "(id, tenant_id, contract_version, blueprint_id, blueprint_version, "
            "manifest_digest, request_digest, idempotency_key_hash, status, "
            "application_snapshot, applied_by_user_id) VALUES "
            "(:id, :tenant_id, 'tenant.blueprint.application.v1', "
            "'government-core', :version, :manifest_digest, :request_digest, "
            ":idempotency_hash, 'applied', '{}', 1)"
        ),
        {
            "id": receipt_id,
            "tenant_id": tenant_id,
            "version": version,
            "manifest_digest": "b" * 64,
            "request_digest": "c" * 64,
            "idempotency_hash": idempotency_hash,
        },
    )


def test_migration_creates_tenant_scoped_immutable_receipts(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'tenant-blueprints.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    migration = _load_migration()

    assert migration.revision == "20260905_tenant_blueprint_v1"
    assert migration.down_revision == "20260904_geo_execution_v2"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1)'))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "tenant_blueprint_application" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns("tenant_blueprint_application")
        }
        assert {
            "tenant_id",
            "blueprint_id",
            "blueprint_version",
            "manifest_digest",
            "request_digest",
            "idempotency_key_hash",
            "application_snapshot",
            "applied_by_user_id",
        }.issubset(columns)
        assert not {"idempotency_key", "secret", "credential"}.intersection(columns)

        _insert_receipt(connection, receipt_id="receipt-1")
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    receipt_id="receipt-duplicate-version",
                    idempotency_hash="d" * 64,
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    receipt_id="receipt-duplicate-key",
                    version="1.0.1",
                )

        _insert_receipt(
            connection,
            receipt_id="receipt-tenant-2",
            tenant_id=2,
            idempotency_hash="a" * 64,
        )

        with Operations.context(context):
            migration.downgrade()
        assert (
            "tenant_blueprint_application"
            not in sa.inspect(connection).get_table_names()
        )
