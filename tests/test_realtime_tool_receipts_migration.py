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
    / "20260729_add_realtime_tool_call_receipts.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "realtime_tool_receipts_migration",
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
    tenant_id: int = 1,
    session_hash: str | None = None,
    call_hash: str | None = None,
    arguments_hash: str | None = None,
    status: str = "reserved",
    output_text: str | None = None,
    last_error_code: str | None = None,
):
    terminal = status in {"completed", "unknown"}
    connection.execute(
        sa.text(
            "INSERT INTO realtime_tool_call_receipt "
            "(id, tenant_id, session_id_hash, call_id_hash, tool_name, "
            "arguments_hash, effect_idempotency_key, status, output_text, "
            "last_error_code, completed_at) VALUES "
            "(:id, :tenant_id, :session_hash, :call_hash, :tool_name, "
            ":arguments_hash, :effect_key, :status, :output_text, "
            ":last_error_code, :completed_at)"
        ),
        {
            "id": row_id,
            "tenant_id": tenant_id,
            "session_hash": session_hash or f"{row_id:064x}",
            "call_hash": call_hash or f"{row_id + 100:064x}",
            "tool_name": "registrar_solicitud_operativa",
            "arguments_hash": arguments_hash or f"{row_id + 200:064x}",
            "effect_key": f"voice:{row_id:064x}",
            "status": status,
            "output_text": output_text,
            "last_error_code": last_error_code,
            "completed_at": "2026-07-29 14:00:00" if terminal else None,
        },
    )


def test_sqlite_upgrade_enforces_realtime_receipt_contract_and_downgrades(tmp_path):
    database_path = tmp_path / "realtime-tool-receipts.sqlite3"
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
        assert "realtime_tool_call_receipt" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("realtime_tool_call_receipt")
        }
        assert set(columns) == {
            "id",
            "tenant_id",
            "session_id_hash",
            "call_id_hash",
            "tool_name",
            "arguments_hash",
            "effect_idempotency_key",
            "status",
            "output_text",
            "last_error_code",
            "contract_version",
            "reserved_at",
            "completed_at",
            "created_at",
            "updated_at",
        }
        assert "arguments" not in columns
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("realtime_tool_call_receipt")
        }
        assert "ix_realtime_tool_receipt_status_updated" in indexes
        assert "ix_realtime_tool_receipt_tenant_tool" in indexes

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        shared_session = "a" * 64
        shared_call = "b" * 64
        _insert_receipt(
            connection,
            row_id=1,
            session_hash=shared_session,
            call_hash=shared_call,
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(
                    connection,
                    row_id=2,
                    session_hash=shared_session,
                    call_hash=shared_call,
                )

        _insert_receipt(
            connection,
            row_id=3,
            tenant_id=2,
            session_hash=shared_session,
            call_hash=shared_call,
        )
        _insert_receipt(
            connection,
            row_id=4,
            session_hash="c" * 64,
            call_hash=shared_call,
        )

        invalid_rows = [
            {"row_id": 5, "arguments_hash": "short"},
            {
                "row_id": 6,
                "status": "completed",
                "output_text": None,
            },
            {
                "row_id": 7,
                "status": "unknown",
                "output_text": "{}",
                "last_error_code": None,
            },
            {
                "row_id": 8,
                "status": "completed",
                "output_text": "x" * 4097,
            },
        ]
        for values in invalid_rows:
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    _insert_receipt(connection, **values)

        _insert_receipt(
            connection,
            row_id=9,
            status="completed",
            output_text="resultado estable",
        )
        _insert_receipt(
            connection,
            row_id=10,
            status="unknown",
            output_text='{"ok":false}',
            last_error_code="tool_execution_unknown",
        )

        with Operations.context(context):
            migration.downgrade()
        assert "realtime_tool_call_receipt" not in sa.inspect(connection).get_table_names()

    engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://"])
def test_realtime_tool_receipt_migration_compiles_offline(url):
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
    assert "CREATE TABLE realtime_tool_call_receipt" in upgrade_sql
    assert "uq_realtime_tool_receipt_scope_call" in upgrade_sql
    assert "arguments_hash" in upgrade_sql
    assert "arguments TEXT" not in upgrade_sql
    assert "ix_realtime_tool_receipt_status_updated" in upgrade_sql

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
    assert "DROP TABLE realtime_tool_call_receipt" in downgrade_buffer.getvalue()


def test_domain_effect_outbox_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260802_whatsapp_workflow_v1"
    ]
