from __future__ import annotations

import importlib.util
import json
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
    ROOT / "migrations" / "versions" / "20260729_add_domain_effect_outbox.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "domain_effect_outbox_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_effect(
    connection,
    *,
    row_id: int,
    tenant_id: int = 1,
    aggregate_type: str = "municipio_ticket",
    aggregate_ref: str = "10",
    effect_type: str = "ticket.created.notify",
    handler_name: str = "notify_ticket_created",
    channel: str = "internal",
    recipient_ref: str = "tenant_contact:operations",
    effect_key: str | None = None,
    intent_hmac: str | None = None,
    status: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = 8,
    lease_token: str | None = None,
    leased_until: str | None = None,
    io_started_at: str | None = None,
    provider_ref_hash: str | None = None,
    processed_at: str | None = None,
    last_error_code: str | None = None,
    last_error_digest: str | None = None,
):
    connection.execute(
        sa.text(
            "INSERT INTO domain_effect_outbox "
            "(id, tenant_id, aggregate_type, aggregate_ref, effect_type, "
            "handler_name, channel, recipient_ref, effect_key, intent_hmac, "
            "status, attempt_count, max_attempts, lease_token, leased_until, "
            "io_started_at, provider_ref_hash, processed_at, last_error_code, "
            "last_error_digest) VALUES "
            "(:id, :tenant_id, :aggregate_type, :aggregate_ref, :effect_type, "
            ":handler_name, :channel, :recipient_ref, :effect_key, :intent_hmac, "
            ":status, :attempt_count, :max_attempts, :lease_token, :leased_until, "
            ":io_started_at, :provider_ref_hash, :processed_at, :last_error_code, "
            ":last_error_digest)"
        ),
        {
            "id": row_id,
            "tenant_id": tenant_id,
            "aggregate_type": aggregate_type,
            "aggregate_ref": aggregate_ref,
            "effect_type": effect_type,
            "handler_name": handler_name,
            "channel": channel,
            "recipient_ref": recipient_ref,
            "effect_key": effect_key or f"effect:{row_id}",
            "intent_hmac": intent_hmac or f"{row_id:064x}",
            "status": status,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "lease_token": lease_token,
            "leased_until": leased_until,
            "io_started_at": io_started_at,
            "provider_ref_hash": provider_ref_hash,
            "processed_at": processed_at,
            "last_error_code": last_error_code,
            "last_error_digest": last_error_digest,
        },
    )


def test_sqlite_upgrade_enforces_domain_effect_contract_and_downgrades(tmp_path):
    database_path = tmp_path / "domain-effect-outbox.sqlite3"
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
        assert "domain_effect_outbox" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("domain_effect_outbox")
        }
        assert set(columns) == {
            "id",
            "tenant_id",
            "aggregate_type",
            "aggregate_ref",
            "effect_type",
            "handler_name",
            "channel",
            "recipient_ref",
            "effect_key",
            "intent_hmac",
            "payload_json",
            "status",
            "attempt_count",
            "max_attempts",
            "available_at",
            "lease_token",
            "leased_until",
            "io_started_at",
            "provider_ref_hash",
            "processed_at",
            "result_json",
            "last_error_code",
            "last_error_digest",
            "contract_version",
            "created_at",
            "updated_at",
        }
        assert not {
            "email",
            "phone",
            "telefono",
            "recipient",
            "citizen_text",
            "address",
        }.intersection(columns)

        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("domain_effect_outbox")
        }
        assert {
            "ix_domain_effect_due",
            "ix_domain_effect_stale",
            "ix_domain_effect_aggregate",
            "ix_domain_effect_tenant_status",
        }.issubset(indexes)
        assert indexes["ix_domain_effect_due"]["column_names"] == [
            "status",
            "available_at",
            "id",
        ]
        assert indexes["ix_domain_effect_stale"]["column_names"] == [
            "status",
            "leased_until",
            "id",
        ]
        assert indexes["ix_domain_effect_aggregate"]["column_names"] == [
            "tenant_id",
            "aggregate_type",
            "aggregate_ref",
            "id",
        ]
        assert indexes["ix_domain_effect_tenant_status"]["column_names"] == [
            "tenant_id",
            "status",
            "updated_at",
            "id",
        ]

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        _insert_effect(connection, row_id=1, effect_key="shared-effect")
        defaults = connection.execute(
            sa.text(
                "SELECT payload_json, status, attempt_count, max_attempts, "
                "available_at, contract_version, created_at, updated_at "
                "FROM domain_effect_outbox WHERE id = 1"
            )
        ).mappings().one()
        payload = defaults["payload_json"]
        decoded_payload = json.loads(payload) if isinstance(payload, str) else payload
        assert decoded_payload == {}
        assert defaults["status"] == "pending"
        assert defaults["attempt_count"] == 0
        assert defaults["max_attempts"] == 8
        assert defaults["available_at"] is not None
        assert defaults["contract_version"] == "domain.effect_outbox.v1"
        assert defaults["created_at"] is not None
        assert defaults["updated_at"] is not None

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_effect(connection, row_id=2, effect_key="shared-effect")
        _insert_effect(
            connection,
            row_id=3,
            tenant_id=2,
            effect_key="shared-effect",
        )

        invalid_rows = [
            {"row_id": 8, "aggregate_ref": " aggregate:8 "},
            {"row_id": 9, "aggregate_type": " future_type "},
            {"row_id": 10, "aggregate_type": ""},
            {"row_id": 11, "aggregate_ref": ""},
            {"row_id": 12, "effect_type": ""},
            {"row_id": 13, "effect_type": " effect.with.spaces "},
            {"row_id": 14, "handler_name": ""},
            {"row_id": 15, "handler_name": " handler_with_spaces "},
            {"row_id": 16, "channel": "push"},
            {"row_id": 17, "recipient_ref": ""},
            {"row_id": 18, "effect_key": " effect:18 "},
            {"row_id": 19, "intent_hmac": "short"},
            {"row_id": 20, "intent_hmac": "g" * 64},
            {"row_id": 21, "intent_hmac": "A" * 64},
            {"row_id": 22, "status": "sending"},
            {"row_id": 23, "attempt_count": -1},
            {"row_id": 24, "attempt_count": 9, "max_attempts": 8},
            {"row_id": 25, "max_attempts": 0},
            {"row_id": 26, "status": "processing"},
            {
                "row_id": 27,
                "status": "pending",
                "lease_token": "lease-27",
                "leased_until": "2026-07-29 23:05:00",
            },
            {
                "row_id": 28,
                "status": "processing",
                "lease_token": "",
                "leased_until": "2026-07-29 23:05:00",
            },
            {"row_id": 29, "status": "succeeded"},
            {
                "row_id": 30,
                "status": "pending",
                "processed_at": "2026-07-29 23:05:00",
            },
            {
                "row_id": 31,
                "status": "retry_wait",
                "io_started_at": "2026-07-29 23:04:00",
            },
            {"row_id": 32, "provider_ref_hash": "short"},
            {"row_id": 33, "provider_ref_hash": "z" * 64},
            {"row_id": 34, "last_error_code": " bad-code "},
            {"row_id": 35, "last_error_digest": "F" * 64},
        ]
        for values in invalid_rows:
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    _insert_effect(connection, **values)

        _insert_effect(
            connection,
            row_id=40,
            status="processing",
            attempt_count=1,
            lease_token="lease-40",
            leased_until="2026-07-29 23:10:00",
            io_started_at="2026-07-29 23:04:00",
        )
        _insert_effect(
            connection,
            row_id=39,
            aggregate_type="order",
            aggregate_ref="4fac2e9c-8f47-4f66-8347-257fb2532d6b",
            effect_type="market.order.created",
        )
        for row_id, status in enumerate(
            ("succeeded", "skipped", "unknown", "dead"),
            start=41,
        ):
            _insert_effect(
                connection,
                row_id=row_id,
                status=status,
                attempt_count=1,
                io_started_at="2026-07-29 23:04:00",
                provider_ref_hash="a" * 64,
                processed_at="2026-07-29 23:05:00",
                last_error_code="provider_unknown" if status == "unknown" else None,
                last_error_digest="b" * 64 if status == "unknown" else None,
            )

        connection.execute(sa.text("DELETE FROM tenant_profile WHERE id = 2"))
        assert connection.execute(
            sa.text("SELECT COUNT(*) FROM domain_effect_outbox WHERE tenant_id = 2")
        ).scalar_one() == 0

        with Operations.context(context):
            migration.downgrade()
        assert "domain_effect_outbox" not in sa.inspect(connection).get_table_names()

    engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://"])
def test_domain_effect_outbox_migration_compiles_offline(url):
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
    assert "CREATE TABLE domain_effect_outbox" in upgrade_sql
    assert "uq_domain_effect_tenant_key" in upgrade_sql
    assert "ck_domain_effect_lease_state" in upgrade_sql
    assert "ck_domain_effect_terminal_time" in upgrade_sql
    assert "ck_domain_effect_io_state" in upgrade_sql
    assert "ix_domain_effect_due" in upgrade_sql
    assert "ix_domain_effect_stale" in upgrade_sql
    assert "recipient_ref" in upgrade_sql
    assert "recipient_email" not in upgrade_sql
    assert "recipient_phone" not in upgrade_sql

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
    assert "DROP TABLE domain_effect_outbox" in downgrade_buffer.getvalue()


def test_domain_effect_outbox_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260815_tenant_reply_event_v1"
    ]
