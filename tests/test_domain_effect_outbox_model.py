from __future__ import annotations

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CheckConstraint, CreateTable, UniqueConstraint

from models import DomainEffectOutbox


EXPECTED_COLUMNS = {
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


def test_domain_effect_outbox_model_exposes_the_durable_privacy_safe_contract():
    table = DomainEffectOutbox.__table__

    assert table.name == "domain_effect_outbox"
    assert set(table.columns.keys()) == EXPECTED_COLUMNS
    assert DomainEffectOutbox.CONTRACT_VERSION == "domain.effect_outbox.v1"
    assert DomainEffectOutbox.CHANNELS == (
        "sigem",
        "email",
        "sms",
        "whatsapp",
        "realtime",
        "internal",
    )
    assert DomainEffectOutbox.STATUSES == (
        "pending",
        "processing",
        "retry_wait",
        "succeeded",
        "skipped",
        "unknown",
        "dead",
    )
    assert DomainEffectOutbox.TERMINAL_STATUSES == (
        "succeeded",
        "skipped",
        "unknown",
        "dead",
    )

    assert table.c.aggregate_type.type.length == 48
    assert table.c.aggregate_ref.type.length == 191
    assert table.c.effect_type.type.length == 96
    assert table.c.handler_name.type.length == 96
    assert table.c.channel.type.length == 16
    assert table.c.recipient_ref.type.length == 191
    assert table.c.effect_key.type.length == 191
    assert table.c.intent_hmac.type.length == 64
    assert table.c.lease_token.type.length == 64
    assert table.c.provider_ref_hash.type.length == 64
    assert table.c.last_error_code.type.length == 64
    assert table.c.last_error_digest.type.length == 64

    assert table.c.payload_json.nullable is False
    assert table.c.payload_json.default is not None
    assert table.c.payload_json.server_default is not None
    assert table.c.status.default.arg == "pending"
    assert table.c.attempt_count.default.arg == 0
    assert table.c.max_attempts.default.arg == 8
    assert table.c.contract_version.default.arg == "domain.effect_outbox.v1"

    foreign_key = next(iter(table.c.tenant_id.foreign_keys))
    assert foreign_key.target_fullname == "tenant_profile.id"
    assert foreign_key.ondelete == "CASCADE"

    sensitive_columns = {
        "email",
        "phone",
        "telefono",
        "recipient",
        "recipient_email",
        "recipient_phone",
        "citizen_text",
        "address",
    }
    assert not sensitive_columns.intersection(table.columns.keys())


def test_domain_effect_outbox_model_names_all_constraints_and_worker_indexes():
    table = DomainEffectOutbox.__table__
    check_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert check_names == {
        "ck_domain_effect_aggregate_type",
        "ck_domain_effect_aggregate_ref",
        "ck_domain_effect_effect_type",
        "ck_domain_effect_handler_name",
        "ck_domain_effect_channel",
        "ck_domain_effect_recipient_ref",
        "ck_domain_effect_effect_key",
        "ck_domain_effect_intent_hmac",
        "ck_domain_effect_status",
        "ck_domain_effect_attempts",
        "ck_domain_effect_lease_token",
        "ck_domain_effect_lease_state",
        "ck_domain_effect_terminal_time",
        "ck_domain_effect_io_state",
        "ck_domain_effect_provider_ref_hash",
        "ck_domain_effect_error_code",
        "ck_domain_effect_error_digest",
    }

    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints["uq_domain_effect_tenant_key"] == (
        "tenant_id",
        "effect_key",
    )

    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    }
    assert indexes == {
        "ix_domain_effect_due": ("status", "available_at", "id"),
        "ix_domain_effect_stale": ("status", "leased_until", "id"),
        "ix_domain_effect_aggregate": (
            "tenant_id",
            "aggregate_type",
            "aggregate_ref",
            "id",
        ),
        "ix_domain_effect_tenant_status": (
            "tenant_id",
            "status",
            "updated_at",
            "id",
        ),
    }


def test_domain_effect_outbox_model_compiles_for_sqlite_and_postgresql():
    table = DomainEffectOutbox.__table__
    sqlite_sql = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    postgres_sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))

    for sql in (sqlite_sql, postgres_sql):
        assert "domain_effect_outbox" in sql
        assert "ck_domain_effect_lease_state" in sql
        assert "ck_domain_effect_terminal_time" in sql
        assert "ck_domain_effect_io_state" in sql
        assert "uq_domain_effect_tenant_key" in sql
    assert "JSON" in sqlite_sql
    assert "JSONB" in postgres_sql
