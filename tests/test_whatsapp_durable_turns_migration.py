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
    ROOT / "migrations" / "versions" / "20260729_add_whatsapp_durable_turns.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "whatsapp_durable_turns_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "provider_connection",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "provider_sender",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    return metadata


def _insert_inbound(
    connection,
    *,
    row_id: int,
    tenant_id: int,
    sid: str,
    stream_key: str,
    status: str | None = None,
) -> None:
    values = {
        "id": row_id,
        "turn_id": f"00000000-0000-0000-0000-{row_id:012d}",
        "tenant_id": tenant_id,
        "provider_message_sid": sid,
        "stream_key": stream_key,
        "message_kind": "text",
        "payload_digest": f"{row_id:064x}",
        "payload_json": "{}",
    }
    if status is not None:
        values.update(
            status=status,
            lease_token=f"lease-{row_id}",
            leased_until="2026-07-29 12:05:00",
        )
    columns = ", ".join(values)
    parameters = ", ".join(f":{column}" for column in values)
    connection.execute(
        sa.text(
            f"INSERT INTO whatsapp_inbound_turn ({columns}) VALUES ({parameters})"
        ),
        values,
    )


def _insert_outbound(
    connection,
    *,
    row_id: int,
    inbound_turn_id: int,
    tenant_id: int,
    sequence_no: int,
    stream_key: str,
    status: str | None = None,
    provider_message_sid: str | None = None,
) -> None:
    values = {
        "id": row_id,
        "attempt_id": f"10000000-0000-0000-0000-{row_id:012d}",
        "tenant_id": tenant_id,
        "inbound_turn_id": inbound_turn_id,
        "stream_key": stream_key,
        "sequence_no": sequence_no,
        "message_kind": "text",
        "idempotency_key": f"attempt-{tenant_id}-{row_id}",
        "payload_digest": f"{row_id + 100:064x}",
        "payload_json": "{}",
    }
    if status is not None:
        values["status"] = status
    if status == "sending":
        values["lease_token"] = f"send-lease-{row_id}"
        values["leased_until"] = "2026-07-29 12:05:00"
    if provider_message_sid is not None:
        values["provider_message_sid"] = provider_message_sid
    columns = ", ".join(values)
    parameters = ", ".join(f":{column}" for column in values)
    connection.execute(
        sa.text(
            f"INSERT INTO whatsapp_outbound_attempt ({columns}) "
            f"VALUES ({parameters})"
        ),
        values,
    )


def test_sqlite_upgrade_enforces_durable_turn_contract_and_downgrades(tmp_path):
    database_path = tmp_path / "whatsapp-durable-turns-migration.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    _base_metadata().create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert {
            "tenant_profile",
            "provider_connection",
            "provider_sender",
            "whatsapp_inbound_turn",
            "whatsapp_outbound_attempt",
        } == set(inspector.get_table_names())

        inbound_columns = {
            column["name"]: column
            for column in inspector.get_columns("whatsapp_inbound_turn")
        }
        assert {
            "id",
            "turn_id",
            "tenant_id",
            "provider_connection_id",
            "provider_sender_id",
            "provider",
            "provider_message_sid",
            "stream_key",
            "conversation_id",
            "channel_session_id",
            "chat_session_id",
            "message_kind",
            "payload_digest",
            "payload_json",
            "status",
            "attempt_count",
            "max_attempts",
            "available_at",
            "lease_token",
            "leased_until",
            "received_at",
            "processing_started_at",
            "completed_at",
            "last_error_code",
            "result_json",
            "contract_version",
            "created_at",
            "updated_at",
        } == set(inbound_columns)
        assert "received" in str(inbound_columns["status"]["default"])
        assert "8" in str(inbound_columns["max_attempts"]["default"])

        outbound_columns = {
            column["name"]: column
            for column in inspector.get_columns("whatsapp_outbound_attempt")
        }
        assert {
            "id",
            "attempt_id",
            "tenant_id",
            "inbound_turn_id",
            "provider_connection_id",
            "provider_sender_id",
            "provider",
            "stream_key",
            "sequence_no",
            "message_kind",
            "idempotency_key",
            "payload_digest",
            "payload_json",
            "status",
            "provider_message_sid",
            "provider_status",
            "attempt_count",
            "max_attempts",
            "available_at",
            "lease_token",
            "leased_until",
            "accepted_at",
            "completed_at",
            "last_error_code",
            "last_error_digest",
            "contract_version",
            "created_at",
            "updated_at",
        } == set(outbound_columns)
        assert "pending" in str(outbound_columns["status"]["default"])
        assert "5" in str(outbound_columns["max_attempts"]["default"])

        inbound_indexes = {
            index["name"]: index
            for index in inspector.get_indexes("whatsapp_inbound_turn")
        }
        assert inbound_indexes["uq_whatsapp_inbound_turn_stream_processing"][
            "unique"
        ] == 1
        outbound_indexes = {
            index["name"]: index
            for index in inspector.get_indexes("whatsapp_outbound_attempt")
        }
        assert outbound_indexes["uq_whatsapp_outbound_attempt_provider_message"][
            "unique"
        ] == 1
        assert outbound_indexes["uq_whatsapp_outbound_attempt_stream_sending"][
            "unique"
        ] == 1

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        _insert_inbound(
            connection,
            row_id=1,
            tenant_id=1,
            sid="SMmigrationShared",
            stream_key="migration-stream",
            status="processing",
        )
        _insert_inbound(
            connection,
            row_id=2,
            tenant_id=2,
            sid="SMmigrationShared",
            stream_key="migration-stream",
            status="processing",
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_inbound(
                    connection,
                    row_id=3,
                    tenant_id=1,
                    sid="SMmigrationOther",
                    stream_key="migration-stream",
                    status="processing",
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_inbound(
                    connection,
                    row_id=4,
                    tenant_id=1,
                    sid="SMmigrationShared",
                    stream_key="different-stream",
                )

        _insert_outbound(
            connection,
            row_id=1,
            inbound_turn_id=1,
            tenant_id=1,
            sequence_no=1,
            stream_key="migration-stream",
            status="sending",
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_outbound(
                    connection,
                    row_id=2,
                    inbound_turn_id=1,
                    tenant_id=1,
                    sequence_no=2,
                    stream_key="migration-stream",
                    status="sending",
                )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_outbound(
                    connection,
                    row_id=3,
                    inbound_turn_id=1,
                    tenant_id=1,
                    sequence_no=2,
                    stream_key="other-stream",
                    status="accepted",
                )

        with Operations.context(context):
            migration.downgrade()
        inspector = sa.inspect(connection)
        assert "whatsapp_inbound_turn" not in inspector.get_table_names()
        assert "whatsapp_outbound_attempt" not in inspector.get_table_names()

    engine.dispose()


def test_database_rejects_cross_tenant_outbound_parent_link(tmp_path):
    database_path = tmp_path / "whatsapp-cross-tenant-parent.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    _base_metadata().create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        _insert_inbound(
            connection,
            row_id=1,
            tenant_id=1,
            sid="SMtenantParent",
            stream_key="tenant-parent-stream",
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_outbound(
                    connection,
                    row_id=1,
                    inbound_turn_id=1,
                    tenant_id=2,
                    sequence_no=1,
                    stream_key="tenant-parent-stream",
                )

    engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://"])
def test_migration_compiles_offline_with_partial_fences(url):
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
    assert "CREATE TABLE whatsapp_inbound_turn" in upgrade_sql
    assert "CREATE TABLE whatsapp_outbound_attempt" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_whatsapp_inbound_turn_stream_processing" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_whatsapp_outbound_attempt_stream_sending" in upgrade_sql
    assert "WHERE status = 'processing'" in upgrade_sql
    assert "WHERE status = 'sending'" in upgrade_sql
    if url.startswith("postgresql"):
        assert "JSONB NOT NULL" in upgrade_sql

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
    downgrade_sql = downgrade_buffer.getvalue()
    assert "DROP TABLE whatsapp_outbound_attempt" in downgrade_sql
    assert "DROP TABLE whatsapp_inbound_turn" in downgrade_sql


def test_domain_effect_outbox_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["20260820_survey_content_jurisdiction_v1"]
