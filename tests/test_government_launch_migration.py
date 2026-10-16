from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

from scripts.preflight_neon_cutover import REVIEWED_MIGRATION_HEAD


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260905_add_government_launch_receipt_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "government_launch_receipt_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_launch_receipt(
    connection,
    *,
    receipt_id: str,
    tenant_id: int = 1,
    application_id: str = "application-1",
    idempotency_hash: str = "a" * 64,
    launch_id: str = "mesa-unica",
):
    connection.execute(
        sa.text(
            "INSERT INTO tenant_blueprint_launch_receipt "
            "(id, tenant_id, blueprint_application_id, contract_version, "
            "blueprint_id, blueprint_version, launch_id, manifest_digest, "
            "launch_digest, request_digest, idempotency_key_hash, status, "
            "application_snapshot, applied_by_user_id) VALUES "
            "(:id, :tenant_id, :application_id, "
            "'tenant.blueprint.launch.receipt.v1', 'government-core', '1.0.0', "
            ":launch_id, :manifest_digest, :launch_digest, :request_digest, "
            ":idempotency_hash, 'applied', '{}', 1)"
        ),
        {
            "id": receipt_id,
            "tenant_id": tenant_id,
            "application_id": application_id,
            "launch_id": launch_id,
            "manifest_digest": "b" * 64,
            "launch_digest": "c" * 64,
            "request_digest": "d" * 64,
            "idempotency_hash": idempotency_hash,
        },
    )


def test_government_launch_migration_is_linear_tenant_scoped_and_append_only(
):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "tenant_blueprint_application",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    assert migration.revision == "20260905_government_launch_v1"
    assert migration.down_revision == "20260905_tenant_blueprint_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1)'))
        connection.execute(
            sa.text(
                "INSERT INTO tenant_blueprint_application (id, tenant_id) "
                "VALUES ('application-1', 1), ('application-2', 2)"
            )
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "tenant_blueprint_launch_receipt" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns("tenant_blueprint_launch_receipt")
        }
        assert {
            "tenant_id",
            "blueprint_application_id",
            "blueprint_id",
            "blueprint_version",
            "launch_id",
            "manifest_digest",
            "launch_digest",
            "request_digest",
            "idempotency_key_hash",
            "application_snapshot",
            "applied_by_user_id",
        }.issubset(columns)
        assert not {"idempotency_key", "secret", "credential"}.intersection(columns)

        _insert_launch_receipt(connection, receipt_id="launch-1")
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_launch_receipt(
                    connection,
                    receipt_id="launch-same-module",
                    idempotency_hash="e" * 64,
                )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_launch_receipt(
                    connection,
                    receipt_id="launch-same-key",
                    launch_id="another-module",
                )

        _insert_launch_receipt(
            connection,
            receipt_id="launch-tenant-2",
            tenant_id=2,
            application_id="application-2",
        )

        with pytest.raises(sa.exc.DBAPIError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE tenant_blueprint_launch_receipt "
                        "SET status = 'applied' WHERE id = 'launch-1'"
                    )
                )
        with pytest.raises(sa.exc.DBAPIError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "DELETE FROM tenant_blueprint_launch_receipt "
                        "WHERE id = 'launch-1'"
                    )
                )

        with Operations.context(context):
            migration.downgrade()
        assert "tenant_blueprint_launch_receipt" not in sa.inspect(
            connection
        ).get_table_names()


def test_government_launch_migration_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVIEWED_MIGRATION_HEAD]
