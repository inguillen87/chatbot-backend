from __future__ import annotations

from datetime import datetime, timezone
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
    / "20260802_add_notification_whatsapp_transport_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "notification_whatsapp_transport_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_schema(engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    tenant = sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    registry = sa.Table(
        "message_template_registry",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
    )
    connection = sa.Table(
        "provider_connection",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
    )
    sender = sa.Table(
        "provider_sender",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column(
            "provider_connection_id",
            sa.Integer(),
            sa.ForeignKey("provider_connection.id"),
            nullable=True,
        ),
    )
    event = sa.Table(
        "messaging_event_ledger",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
    )
    template = sa.Table(
        "notification_template",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("subject_template", sa.String(255), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "key",
            "channel",
            name="uq_notification_template_tenant_key_channel",
        ),
    )
    notification = sa.Table(
        "notification",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column(
            "template_id",
            sa.String(36),
            sa.ForeignKey("notification_template.id"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_notification_tenant_idempotency",
        ),
    )
    attempt = sa.Table(
        "notification_attempt",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "notification_id",
            sa.String(36),
            sa.ForeignKey("notification.id"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(40), nullable=True),
        sa.Column("provider_message_id", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    sa.Index("ix_notification_template_tenant_id", template.c.tenant_id)
    sa.Index("ix_notification_tenant_id", notification.c.tenant_id)
    sa.Index("ix_notification_channel", notification.c.channel)
    sa.Index("ix_notification_status", notification.c.status)
    sa.Index("ix_notification_next_retry_at", notification.c.next_retry_at)
    sa.Index("ix_notification_attempt_notification_id", attempt.c.notification_id)
    sa.Index("ix_notification_attempt_tenant_id", attempt.c.tenant_id)
    metadata.create_all(engine)
    return {
        "tenant": tenant,
        "user": user,
        "registry": registry,
        "connection": connection,
        "sender": sender,
        "event": event,
        "template": template,
        "notification": notification,
        "attempt": attempt,
    }


def _seed_legacy_rows(
    connection,
    tables: dict[str, sa.Table],
    *,
    notification_status: str = "blocked",
    duplicate_attempt: bool = False,
) -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    connection.execute(tables["tenant"].insert(), [{"id": 1}, {"id": 2}])
    connection.execute(
        tables["user"].insert(),
        [{"id": 10, "tenant_id": 1}, {"id": 20, "tenant_id": 2}],
    )
    connection.execute(
        tables["registry"].insert(),
        [{"id": 101, "tenant_id": 1}, {"id": 201, "tenant_id": 2}],
    )
    connection.execute(
        tables["connection"].insert(),
        [{"id": 111, "tenant_id": 1}, {"id": 211, "tenant_id": 2}],
    )
    connection.execute(
        tables["sender"].insert(),
        [
            {"id": 121, "tenant_id": 1, "provider_connection_id": 111},
            {"id": 221, "tenant_id": 2, "provider_connection_id": 211},
        ],
    )
    connection.execute(
        tables["event"].insert(),
        [{"id": 131, "tenant_id": 1}, {"id": 231, "tenant_id": 2}],
    )
    connection.execute(
        tables["template"].insert().values(
            id="tpl-1",
            tenant_id=1,
            key="claim_created",
            channel="whatsapp",
            body_template="Reclamo ${claim_id}",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    connection.execute(
        tables["notification"].insert().values(
            id="notification-1",
            tenant_id=1,
            user_id=10,
            template_id="tpl-1",
            channel="whatsapp",
            recipient="whatsapp:+5492600000000",
            body="Reclamo creado",
            status=notification_status,
            idempotency_key="claim-created-1",
            max_retries=3,
            attempt_count=1,
            created_at=now,
            updated_at=now,
        )
    )
    attempts = [
        {
            "id": "attempt-1",
            "notification_id": "notification-1",
            "tenant_id": 1,
            "attempt_number": 1,
            "status": "blocked",
            "provider": "whatsapp",
            "attempted_at": now,
            "created_at": now,
            "updated_at": now,
        }
    ]
    if duplicate_attempt:
        attempts.append(
            {
                **attempts[0],
                "id": "attempt-duplicate",
            }
        )
    connection.execute(tables["attempt"].insert(), attempts)


def _run_upgrade(connection, migration) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        with context.begin_transaction():
            migration.upgrade()


def _run_downgrade(connection, migration) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        with context.begin_transaction():
            migration.downgrade()


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _insert_notification(connection, table, **overrides) -> None:
    now = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)
    values = {
        "id": "notification-valid",
        "tenant_id": 1,
        "user_id": 10,
        "template_id": "tpl-1",
        "channel": "whatsapp",
        "recipient": "whatsapp:+5492611111111",
        "body": "Plantilla aprobada",
        "status": "queued",
        "idempotency_key": "transport-valid",
        "max_retries": 3,
        "attempt_count": 0,
        "provider_status": "unknown",
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    connection.execute(table.insert().values(**values))


def _insert_attempt(connection, table, **overrides) -> None:
    now = datetime(2026, 8, 2, 12, 31, tzinfo=timezone.utc)
    values = {
        "id": "attempt-valid",
        "notification_id": "notification-valid",
        "tenant_id": 1,
        "attempt_number": 1,
        "status": "send_uncertain",
        "provider": "twilio",
        "provider_status": "accepted",
        "attempted_at": now,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    connection.execute(table.insert().values(**values))


def _assert_integrity_error(connection, statement) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(statement)


def test_transport_migration_preserves_blocked_rows_and_round_trips(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'notification-transport.sqlite3').as_posix()}"
    )
    tables = _create_legacy_schema(engine)
    migration = _load_migration()

    assert migration.revision == "20260802_notification_wa_v1"
    assert migration.down_revision == "20260801_interview_delivery_v4"

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        _seed_legacy_rows(connection, tables)
        connection.commit()
        _run_upgrade(connection, migration)
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        inspector = sa.inspect(connection)
        assert {
            "message_template_registry_id",
            "provider_connection_id",
            "provider_sender_id",
            "sender_binding",
            "content_sid",
            "content_variables",
            "payload_digest",
            "provider_message_id",
            "provider_status",
            "lease_token",
            "leased_until",
        } <= _column_names(inspector, "notification")
        assert {
            "provider_status",
            "error_digest",
            "delivery_event_id",
        } <= _column_names(inspector, "notification_attempt")
        assert "message_template_registry_id" in _column_names(
            inspector, "notification_template"
        )

        assert {
            check["name"] for check in inspector.get_check_constraints("notification")
        } >= {
            "ck_notification_status",
            "ck_notification_provider_status",
            "ck_notification_sender_binding_length",
            "ck_notification_payload_digest_length",
            "ck_notification_lease_state",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("notification_attempt")
        } >= {"uq_notification_attempt_number"}
        assert {
            index["name"] for index in inspector.get_indexes("notification")
        } >= {
            "ix_notification_tenant_due",
            "ix_notification_provider_message_id",
            "ix_notification_leased_until",
        }

        row = connection.execute(
            sa.text(
                "SELECT status, provider_status, lease_token, leased_until "
                "FROM notification WHERE id = 'notification-1'"
            )
        ).mappings().one()
        assert dict(row) == {
            "status": "blocked",
            "provider_status": "unknown",
            "lease_token": None,
            "leased_until": None,
        }
        attempt_row = connection.execute(
            sa.text(
                "SELECT status, provider_status FROM notification_attempt "
                "WHERE id = 'attempt-1'"
            )
        ).mappings().one()
        assert dict(attempt_row) == {
            "status": "blocked",
            "provider_status": "unknown",
        }

        connection.commit()
        _run_downgrade(connection, migration)
        connection.commit()
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        downgraded = sa.inspect(connection)
        assert "provider_status" not in _column_names(downgraded, "notification")
        assert "delivery_event_id" not in _column_names(
            downgraded, "notification_attempt"
        )
        assert "message_template_registry_id" not in _column_names(
            downgraded, "notification_template"
        )
        assert connection.execute(
            sa.text("SELECT status FROM notification WHERE id = 'notification-1'")
        ).scalar_one() == "blocked"

    engine.dispose()


def test_transport_constraints_reject_ambiguous_or_malformed_state(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'notification-constraints.sqlite3').as_posix()}"
    )
    legacy_tables = _create_legacy_schema(engine)
    migration = _load_migration()

    with engine.connect() as connection:
        _seed_legacy_rows(connection, legacy_tables)
        connection.commit()
        _run_upgrade(connection, migration)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()

        reflected = sa.MetaData()
        notification = sa.Table(
            "notification", reflected, autoload_with=connection
        )
        attempt = sa.Table(
            "notification_attempt", reflected, autoload_with=connection
        )

        _insert_notification(
            connection,
            notification,
            status="sending",
            message_template_registry_id=101,
            provider_connection_id=111,
            provider_sender_id=121,
            sender_binding="a" * 64,
            content_sid="HXapproved-template",
            content_variables={"1": "Marcelo", "2": "401746"},
            payload_digest="b" * 64,
            provider_message_id="SM-provider-1",
            provider_status="accepted",
            lease_token="lease-1",
            leased_until=datetime(2026, 8, 2, 12, 35, tzinfo=timezone.utc),
        )
        _insert_attempt(
            connection,
            attempt,
            error_digest="c" * 64,
            delivery_event_id=131,
        )

        base = {
            "tenant_id": 1,
            "channel": "whatsapp",
            "recipient": "whatsapp:+5492699999999",
            "body": "invalid",
            "max_retries": 3,
            "attempt_count": 0,
            "provider_status": "unknown",
            "created_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
        }

        def invalid_notification(row_id: str, **values):
            payload = {
                **base,
                "id": row_id,
                "idempotency_key": row_id,
                "status": "queued",
                **values,
            }
            _assert_integrity_error(
                connection,
                notification.insert().values(**payload),
            )

        invalid_notification("bad-status", status="invented")
        invalid_notification("bad-provider-status", provider_status="magic")
        invalid_notification("sending-without-lease", status="sending")
        invalid_notification(
            "queued-with-lease",
            lease_token="lease",
            leased_until=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        )
        invalid_notification("short-payload-digest", payload_digest="short")
        invalid_notification("short-sender-binding", sender_binding="short")
        invalid_notification("dangling-sender", provider_sender_id=999999)
        invalid_notification(
            "duplicate-provider-id",
            provider_sender_id=121,
            provider_message_id="SM-provider-1",
        )

        _insert_notification(
            connection,
            notification,
            id="notification-other-tenant",
            tenant_id=2,
            user_id=20,
            template_id=None,
            recipient="whatsapp:+5492622222222",
            idempotency_key="transport-other-tenant",
            provider_sender_id=221,
            provider_message_id="SM-provider-1",
        )

        def invalid_attempt(row_id: str, attempt_number: int, **values):
            payload = {
                "id": row_id,
                "notification_id": "notification-valid",
                "tenant_id": 1,
                "attempt_number": attempt_number,
                "status": "failed",
                "provider_status": "unknown",
                "attempted_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
                "created_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
                **values,
            }
            _assert_integrity_error(connection, attempt.insert().values(**payload))

        invalid_attempt("duplicate-attempt-number", 1)
        invalid_attempt("zero-attempt-number", 0)
        invalid_attempt("bad-attempt-status", 2, status="invented")
        invalid_attempt("bad-attempt-provider-status", 3, provider_status="magic")
        invalid_attempt("short-error-digest", 4, error_digest="short")
        invalid_attempt("dangling-delivery-event", 5, delivery_event_id=999999)

        connection.commit()

    engine.dispose()


@pytest.mark.parametrize(
    ("notification_status", "duplicate_attempt", "message"),
    [
        ("invented", False, "unsupported legacy notification.status"),
        ("sending", False, "legacy sending notifications have no durable lease"),
        ("blocked", True, "duplicate legacy notification attempt numbers"),
    ],
)
def test_transport_migration_fails_closed_on_ambiguous_legacy_rows(
    tmp_path,
    notification_status,
    duplicate_attempt,
    message,
):
    suffix = (
        "duplicate"
        if duplicate_attempt
        else notification_status
    )
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / f'notification-{suffix}.sqlite3').as_posix()}"
    )
    tables = _create_legacy_schema(engine)
    migration = _load_migration()

    with engine.connect() as connection:
        _seed_legacy_rows(
            connection,
            tables,
            notification_status=notification_status,
            duplicate_attempt=duplicate_attempt,
        )
        connection.commit()
        with pytest.raises(RuntimeError, match=message):
            _run_upgrade(connection, migration)
        connection.rollback()
        assert "provider_status" not in _column_names(
            sa.inspect(connection), "notification"
        )

    engine.dispose()


def test_transport_downgrade_refuses_to_erase_delivery_evidence(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'notification-downgrade.sqlite3').as_posix()}"
    )
    legacy_tables = _create_legacy_schema(engine)
    migration = _load_migration()

    with engine.connect() as connection:
        _seed_legacy_rows(connection, legacy_tables)
        connection.commit()
        _run_upgrade(connection, migration)
        connection.commit()
        connection.execute(
            sa.text(
                "UPDATE notification SET content_sid = 'HXapproved' "
                "WHERE id = 'notification-1'"
            )
        )
        connection.commit()

        with pytest.raises(RuntimeError, match="downgrade would erase"):
            _run_downgrade(connection, migration)
        connection.rollback()
        assert "content_sid" in _column_names(sa.inspect(connection), "notification")

    engine.dispose()
