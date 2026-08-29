from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260829_order_whatsapp_inbound_fifo_by_received_at.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "whatsapp_inbound_fifo_order_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _index_columns(connection) -> list[str]:
    indexes = {
        index["name"]: index
        for index in sa.inspect(connection).get_indexes("whatsapp_inbound_turn")
    }
    return indexes["ix_whatsapp_inbound_turn_stream_fifo"]["column_names"]


def test_fifo_order_migration_replaces_and_restores_index(tmp_path):
    migration = _load_migration()
    assert migration.revision == "20260829_inbound_fifo_v2"
    assert migration.down_revision == "20260825_chat_idempotency_v1"

    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'whatsapp-inbound-fifo.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    table = sa.Table(
        "whatsapp_inbound_turn",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("stream_key", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        "ix_whatsapp_inbound_turn_stream_fifo",
        table.c.tenant_id,
        table.c.stream_key,
        table.c.id,
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        assert _index_columns(connection) == ["tenant_id", "stream_key", "id"]

        with Operations.context(context):
            migration.upgrade()
        assert _index_columns(connection) == [
            "tenant_id",
            "stream_key",
            "received_at",
            "id",
        ]

        with Operations.context(context):
            migration.downgrade()
        assert _index_columns(connection) == ["tenant_id", "stream_key", "id"]

    engine.dispose()
