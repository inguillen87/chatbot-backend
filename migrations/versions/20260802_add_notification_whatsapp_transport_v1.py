"""Add durable WhatsApp template transport state to notifications.

Revision ID: 20260802_notification_wa_v1
Revises: 20260801_interview_delivery_v4
Create Date: 2026-08-02

The notification orchestrator already persists ``blocked`` rows when a channel
is unavailable.  This revision deliberately preserves that state while adding
the immutable provider/template snapshots and lease fencing needed by a later
transport worker.  No legacy row is guessed or rewritten: unknown historic
states stop the migration for an explicit audit.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260802_notification_wa_v1"
down_revision = "20260801_interview_delivery_v4"
branch_labels = None
depends_on = None


_NOTIFICATION_STATUSES = frozenset(
    {
        "queued",
        "delayed",
        "sending",
        "retry_wait",
        "sent",
        "send_uncertain",
        "failed",
        "blocked",
    }
)
_ATTEMPT_STATUSES = frozenset(
    {
        "success",
        "failed",
        "delayed",
        "blocked",
        "sending",
        "send_uncertain",
    }
)


def _assert_legacy_notification_states_supported() -> None:
    """Fail closed rather than silently coercing an unknown delivery state."""

    bind = op.get_bind()
    checks = (
        ("notification", _NOTIFICATION_STATUSES),
        ("notification_attempt", _ATTEMPT_STATUSES),
    )
    for table_name, allowed in checks:
        values = bind.execute(
            sa.text(f"SELECT DISTINCT status FROM {table_name}")
        ).scalars()
        unsupported = sorted(
            str(value) for value in values if value is not None and value not in allowed
        )
        if unsupported:
            joined = ", ".join(unsupported)
            raise RuntimeError(
                f"unsupported legacy {table_name}.status values: {joined}"
            )

    unfenced_sends = bind.execute(
        sa.text(
            "SELECT id FROM notification WHERE status = 'sending' ORDER BY id"
        )
    ).scalars().all()
    if unfenced_sends:
        sample = ", ".join(
            str(notification_id) for notification_id in unfenced_sends[:10]
        )
        raise RuntimeError(
            "legacy sending notifications have no durable lease evidence and "
            f"require an explicit audit before migration: {sample}"
        )


def _assert_legacy_attempt_numbers_are_unique() -> None:
    """Refuse to invent an ordering for duplicate historic attempts."""

    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT notification_id, attempt_number, COUNT(*) AS duplicate_count "
            "FROM notification_attempt "
            "GROUP BY notification_id, attempt_number "
            "HAVING COUNT(*) > 1 "
            "ORDER BY notification_id, attempt_number"
        )
    ).mappings().all()
    if duplicates:
        sample = ", ".join(
            f"{row['notification_id']}#{row['attempt_number']}x{row['duplicate_count']}"
            for row in duplicates[:10]
        )
        raise RuntimeError(
            "duplicate legacy notification attempt numbers require an explicit "
            f"audit before migration: {sample}"
        )

    nonpositive = bind.execute(
        sa.text(
            "SELECT id, attempt_number FROM notification_attempt "
            "WHERE attempt_number < 1 ORDER BY id"
        )
    ).mappings().all()
    if nonpositive:
        sample = ", ".join(
            f"{row['id']}#{row['attempt_number']}" for row in nonpositive[:10]
        )
        raise RuntimeError(
            "non-positive legacy notification attempt numbers require an explicit "
            f"audit before migration: {sample}"
        )


def _assert_transport_state_empty_for_downgrade() -> None:
    """Do not erase provider evidence or unresolved sends during rollback."""

    bind = op.get_bind()
    populated_templates = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM notification_template "
            "WHERE message_template_registry_id IS NOT NULL"
        )
    ).scalar_one()
    populated_notifications = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM notification WHERE "
            "message_template_registry_id IS NOT NULL "
            "OR provider_connection_id IS NOT NULL "
            "OR provider_sender_id IS NOT NULL "
            "OR sender_binding IS NOT NULL "
            "OR content_sid IS NOT NULL "
            "OR content_variables IS NOT NULL "
            "OR payload_digest IS NOT NULL "
            "OR provider_message_id IS NOT NULL "
            "OR provider_status <> 'unknown' "
            "OR lease_token IS NOT NULL "
            "OR leased_until IS NOT NULL "
            "OR status IN ('sending', 'retry_wait', 'send_uncertain')"
        )
    ).scalar_one()
    populated_attempts = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM notification_attempt WHERE "
            "provider_status <> 'unknown' "
            "OR error_digest IS NOT NULL "
            "OR delivery_event_id IS NOT NULL "
            "OR status IN ('sending', 'send_uncertain') "
            "OR length(provider_message_id) > 120"
        )
    ).scalar_one()
    if populated_templates or populated_notifications or populated_attempts:
        raise RuntimeError(
            "notification WhatsApp transport state is populated; downgrade would "
            "erase delivery evidence or unresolved send state"
        )


def _sqlite_foreign_keys_enabled() -> bool:
    if op.get_bind().dialect.name != "sqlite":
        return False
    return bool(op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar())


def _set_sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    # SQLite cannot rebuild a referenced parent table while FK enforcement is
    # active. This is called after read-only preflight and before batch DDL, so
    # the PRAGMA is still effective with sqlite3's legacy transaction mode.
    bind = op.get_bind()
    desired = 1 if enabled else 0
    statement = f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}"
    bind.exec_driver_sql(statement)
    actual = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar() or 0)
    if actual != desired:
        # sqlite3 may have opened a real transaction during the table rebuild;
        # PRAGMA foreign_keys is then a no-op until the DBAPI transaction ends.
        # SQLite DDL is non-transactional in this Alembic environment, so close
        # that dialect-level transaction and restore enforcement immediately.
        bind.connection.driver_connection.commit()
        bind.exec_driver_sql(statement)
        actual = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar() or 0)
    if actual != desired:
        raise RuntimeError(
            "could not restore SQLite foreign key enforcement"
            if enabled
            else "could not suspend SQLite foreign key enforcement for batch DDL"
        )


def _assert_sqlite_foreign_keys_clean() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    violations = op.get_bind().exec_driver_sql(
        "PRAGMA foreign_key_check"
    ).fetchmany(10)
    if violations:
        raise RuntimeError(
            "notification transport migration produced foreign key violations: "
            f"{violations}"
        )


def upgrade() -> None:
    _assert_legacy_notification_states_supported()
    _assert_legacy_attempt_numbers_are_unique()

    restore_sqlite_foreign_keys = _sqlite_foreign_keys_enabled()
    if restore_sqlite_foreign_keys:
        _set_sqlite_foreign_keys(False)
    try:
        _upgrade_schema()
        _assert_sqlite_foreign_keys_clean()
    finally:
        if restore_sqlite_foreign_keys:
            _set_sqlite_foreign_keys(True)


def _upgrade_schema() -> None:

    with op.batch_alter_table("notification_template") as batch_op:
        batch_op.add_column(
            sa.Column("message_template_registry_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_notification_template_message_template_registry",
            "message_template_registry",
            ["message_template_registry_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_notification_template_message_template_registry_id",
            ["message_template_registry_id"],
            unique=False,
        )

    with op.batch_alter_table("notification") as batch_op:
        batch_op.add_column(
            sa.Column("message_template_registry_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("provider_connection_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("provider_sender_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("sender_binding", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("content_sid", sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("content_variables", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("payload_digest", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("provider_message_id", sa.String(180), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "provider_status",
                sa.String(32),
                nullable=False,
                server_default="unknown",
            )
        )
        batch_op.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True)
        )

        batch_op.create_foreign_key(
            "fk_notification_message_template_registry",
            "message_template_registry",
            ["message_template_registry_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_notification_provider_connection",
            "provider_connection",
            ["provider_connection_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_notification_provider_sender",
            "provider_sender",
            ["provider_sender_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_notification_tenant_sender_provider_message",
            ["tenant_id", "provider_sender_id", "provider_message_id"],
        )
        batch_op.create_check_constraint(
            "ck_notification_status",
            "status IN ('queued', 'delayed', 'sending', 'retry_wait', 'sent', "
            "'send_uncertain', 'failed', 'blocked')",
        )
        batch_op.create_check_constraint(
            "ck_notification_provider_status",
            "provider_status IN ('unknown', 'accepted', 'scheduled', 'queued', "
            "'sending', 'sent', 'delivered', 'read', 'failed', 'undelivered', "
            "'canceled', 'cancelled')",
        )
        batch_op.create_check_constraint(
            "ck_notification_sender_binding_length",
            "sender_binding IS NULL OR length(sender_binding) = 64",
        )
        batch_op.create_check_constraint(
            "ck_notification_payload_digest_length",
            "payload_digest IS NULL OR length(payload_digest) = 64",
        )
        batch_op.create_check_constraint(
            "ck_notification_lease_state",
            "((status = 'sending' AND lease_token IS NOT NULL "
            "AND leased_until IS NOT NULL) OR (status <> 'sending' "
            "AND lease_token IS NULL AND leased_until IS NULL))",
        )
        batch_op.create_index(
            "ix_notification_message_template_registry_id",
            ["message_template_registry_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_notification_provider_connection_id",
            ["provider_connection_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_notification_provider_sender_id",
            ["provider_sender_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_notification_content_sid", ["content_sid"], unique=False
        )
        batch_op.create_index(
            "ix_notification_provider_message_id",
            ["provider_message_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_notification_leased_until", ["leased_until"], unique=False
        )
        batch_op.create_index(
            "ix_notification_tenant_due",
            ["tenant_id", "status", "next_retry_at", "id"],
            unique=False,
        )

    with op.batch_alter_table("notification_attempt") as batch_op:
        batch_op.alter_column(
            "provider_message_id",
            existing_type=sa.String(120),
            type_=sa.String(180),
            existing_nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "provider_status",
                sa.String(32),
                nullable=False,
                server_default="unknown",
            )
        )
        batch_op.add_column(sa.Column("error_digest", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("delivery_event_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_notification_attempt_delivery_event",
            "messaging_event_ledger",
            ["delivery_event_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_notification_attempt_number",
            ["notification_id", "attempt_number"],
        )
        batch_op.create_check_constraint(
            "ck_notification_attempt_status",
            "status IN ('success', 'failed', 'delayed', 'blocked', 'sending', "
            "'send_uncertain')",
        )
        batch_op.create_check_constraint(
            "ck_notification_attempt_provider_status",
            "provider_status IN ('unknown', 'accepted', 'scheduled', 'queued', "
            "'sending', 'sent', 'delivered', 'read', 'failed', 'undelivered', "
            "'canceled', 'cancelled')",
        )
        batch_op.create_check_constraint(
            "ck_notification_attempt_number_positive",
            "attempt_number >= 1",
        )
        batch_op.create_check_constraint(
            "ck_notification_attempt_error_digest_length",
            "error_digest IS NULL OR length(error_digest) = 64",
        )
        batch_op.create_index(
            "ix_notification_attempt_delivery_event_id",
            ["delivery_event_id"],
            unique=False,
        )


def downgrade() -> None:
    _assert_transport_state_empty_for_downgrade()

    restore_sqlite_foreign_keys = _sqlite_foreign_keys_enabled()
    if restore_sqlite_foreign_keys:
        _set_sqlite_foreign_keys(False)
    try:
        _downgrade_schema()
        _assert_sqlite_foreign_keys_clean()
    finally:
        if restore_sqlite_foreign_keys:
            _set_sqlite_foreign_keys(True)


def _downgrade_schema() -> None:

    with op.batch_alter_table("notification_attempt") as batch_op:
        batch_op.drop_index("ix_notification_attempt_delivery_event_id")
        batch_op.drop_constraint(
            "ck_notification_attempt_error_digest_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_notification_attempt_number_positive", type_="check"
        )
        batch_op.drop_constraint(
            "ck_notification_attempt_provider_status", type_="check"
        )
        batch_op.drop_constraint("ck_notification_attempt_status", type_="check")
        batch_op.drop_constraint(
            "fk_notification_attempt_delivery_event", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "uq_notification_attempt_number", type_="unique"
        )
        batch_op.drop_column("delivery_event_id")
        batch_op.drop_column("error_digest")
        batch_op.drop_column("provider_status")
        batch_op.alter_column(
            "provider_message_id",
            existing_type=sa.String(180),
            type_=sa.String(120),
            existing_nullable=True,
        )

    with op.batch_alter_table("notification") as batch_op:
        batch_op.drop_index("ix_notification_tenant_due")
        batch_op.drop_index("ix_notification_leased_until")
        batch_op.drop_index("ix_notification_provider_message_id")
        batch_op.drop_index("ix_notification_content_sid")
        batch_op.drop_index("ix_notification_provider_sender_id")
        batch_op.drop_index("ix_notification_provider_connection_id")
        batch_op.drop_index("ix_notification_message_template_registry_id")
        batch_op.drop_constraint("ck_notification_lease_state", type_="check")
        batch_op.drop_constraint(
            "ck_notification_payload_digest_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_notification_sender_binding_length", type_="check"
        )
        batch_op.drop_constraint("ck_notification_provider_status", type_="check")
        batch_op.drop_constraint("ck_notification_status", type_="check")
        batch_op.drop_constraint(
            "uq_notification_tenant_sender_provider_message", type_="unique"
        )
        batch_op.drop_constraint(
            "fk_notification_provider_sender", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_notification_provider_connection", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_notification_message_template_registry", type_="foreignkey"
        )
        batch_op.drop_column("leased_until")
        batch_op.drop_column("lease_token")
        batch_op.drop_column("provider_status")
        batch_op.drop_column("provider_message_id")
        batch_op.drop_column("payload_digest")
        batch_op.drop_column("content_variables")
        batch_op.drop_column("content_sid")
        batch_op.drop_column("sender_binding")
        batch_op.drop_column("provider_sender_id")
        batch_op.drop_column("provider_connection_id")
        batch_op.drop_column("message_template_registry_id")

    with op.batch_alter_table("notification_template") as batch_op:
        batch_op.drop_index(
            "ix_notification_template_message_template_registry_id"
        )
        batch_op.drop_constraint(
            "fk_notification_template_message_template_registry",
            type_="foreignkey",
        )
        batch_op.drop_column("message_template_registry_id")
