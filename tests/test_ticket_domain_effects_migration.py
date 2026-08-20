from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260729_add_ticket_domain_effect_receipts.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "ticket_domain_effects_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_receipt(
    connection,
    *,
    row_id: int,
    tenant_id: int,
    key: str,
    payload_hash: str | None = None,
    resource_id: int = 1,
    effect_kind: str = "ticket.create.municipio",
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO ticket_domain_effect_receipt "
            "(id, tenant_id, idempotency_key, effect_kind, payload_hash, "
            "resource_type, resource_id, result_json) "
            "VALUES (:id, :tenant_id, :key, :effect_kind, :payload_hash, "
            ":resource_type, :resource_id, :result_json)"
        ),
        {
            "id": row_id,
            "tenant_id": tenant_id,
            "key": key,
            "effect_kind": effect_kind,
            "payload_hash": payload_hash or f"{row_id:064x}",
            "resource_type": "municipio_ticket",
            "resource_id": resource_id,
            "result_json": "{}",
        },
    )


def test_sqlite_upgrade_enforces_ticket_effect_contract_and_downgrades(tmp_path):
    database_path = tmp_path / "ticket-domain-effects.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "ticket_domain_effect_receipt" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("ticket_domain_effect_receipt")
        }
        assert set(columns) == {
            "id",
            "tenant_id",
            "idempotency_key",
            "effect_kind",
            "payload_hash",
            "resource_type",
            "resource_id",
            "result_json",
            "contract_version",
            "created_at",
            "updated_at",
        }
        assert "ticket.domain_effect.v1" in str(columns["contract_version"]["default"])
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("ticket_domain_effect_receipt")
        }
        assert "ix_ticket_domain_effect_tenant_kind" in indexes
        assert "ix_ticket_domain_effect_resource" in indexes

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        _insert_receipt(connection, row_id=1, tenant_id=1, key="shared-key-0001")
        _insert_receipt(connection, row_id=2, tenant_id=2, key="shared-key-0001")

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    row_id=3,
                    tenant_id=1,
                    key="shared-key-0001",
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    row_id=4,
                    tenant_id=1,
                    key="bad-hash-0001",
                    payload_hash="short",
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    row_id=5,
                    tenant_id=1,
                    key="bad-resource-0001",
                    resource_id=0,
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    row_id=6,
                    tenant_id=1,
                    key="bad-effect-0001",
                    effect_kind="unknown.effect",
                )

        with Operations.context(context):
            migration.downgrade()
        assert "ticket_domain_effect_receipt" not in sa.inspect(connection).get_table_names()

    engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://"])
def test_ticket_effect_migration_compiles_offline(url):
    migration = _load_migration_module()
    upgrade_buffer = StringIO()
    upgrade_context = MigrationContext.configure(
        url=url,
        opts={
            "as_sql": True,
            "literal_binds": True,
            "output_buffer": upgrade_buffer,
        },
    )
    with Operations.context(upgrade_context):
        migration.upgrade()
    upgrade_sql = upgrade_buffer.getvalue()
    assert "CREATE TABLE ticket_domain_effect_receipt" in upgrade_sql
    assert "uq_ticket_domain_effect_tenant_idempotency" in upgrade_sql
    assert "ix_ticket_domain_effect_tenant_kind" in upgrade_sql
    if url.startswith("postgresql"):
        assert "result_json JSONB" in upgrade_sql

    downgrade_buffer = StringIO()
    downgrade_context = MigrationContext.configure(
        url=url,
        opts={
            "as_sql": True,
            "literal_binds": True,
            "output_buffer": downgrade_buffer,
        },
    )
    with Operations.context(downgrade_context):
        migration.downgrade()
    assert "DROP TABLE ticket_domain_effect_receipt" in downgrade_buffer.getvalue()


def test_domain_effect_outbox_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260820_survey_response_origin_v1"
    ]
