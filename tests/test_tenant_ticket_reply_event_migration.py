from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260815_add_tenant_ticket_reply_events.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "tenant_ticket_reply_event_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tenant_reply_event_upgrade_adds_append_only_delivery_source(tmp_path):
    database_path = tmp_path / "tenant-ticket-reply-events.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = sa.MetaData()
    tenant_profile = sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    tenant_ticket = sa.Table(
        "tenant_ticket",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant_profile.id"),
            nullable=False,
        ),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(tenant_profile.insert().values(id=1))
        connection.execute(tenant_ticket.insert().values(id=10, tenant_id=1))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "tenant_ticket_reply_event" in inspector.get_table_names()
        columns = {
            item["name"]: item for item in inspector.get_columns(
                "tenant_ticket_reply_event"
            )
        }
        assert columns["body"]["nullable"] is False
        assert columns["recipient_email"]["nullable"] is True
        assert columns["recipient_phone"]["nullable"] is True
        checks = {
            item["name"]: "".join(str(item.get("sqltext") or "").split())
            for item in inspector.get_check_constraints(
                "tenant_ticket_reply_event"
            )
        }
        assert "public" in checks["ck_tenant_ticket_reply_event_visibility"]
        assert "internal" in checks["ck_tenant_ticket_reply_event_visibility"]
        uniques = {
            item["name"] for item in inspector.get_unique_constraints(
                "tenant_ticket_reply_event"
            )
        }
        assert "uq_tenant_ticket_reply_event_tenant_event" in uniques
        indexes = {
            item["name"] for item in inspector.get_indexes(
                "tenant_ticket_reply_event"
            )
        }
        assert "ix_tenant_ticket_reply_event_ticket" in indexes

        connection.execute(
            sa.text(
                "INSERT INTO tenant_ticket_reply_event "
                "(id, tenant_id, ticket_id, event_id, body, visibility, "
                "recipient_email, recipient_phone, contract_version, created_at) "
                "VALUES (1, 1, 10, 'event-1', 'Respuesta durable', 'public', "
                "'citizen@test.com', '+5491111111111', "
                "'tenant_ticket.reply_event.v1', CURRENT_TIMESTAMP)"
            )
        )
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM tenant_ticket_reply_event")
        ) == 1

        with Operations.context(context):
            migration.downgrade()
        assert "tenant_ticket_reply_event" in sa.inspect(connection).get_table_names()
