"""SQLAlchemy Core schema for the independent cutover ingress database."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    text,
)


metadata = MetaData()

schema_revision = Table(
    "cutover_ingress_schema_revision",
    metadata,
    Column("revision", String(64), primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

buffered_whatsapp_ingress = Table(
    "cutover_whatsapp_ingress",
    metadata,
    # Twilio MessageSid is the provider idempotency boundary.  It is globally
    # unique, so making it the primary key makes accidental weaker dedupe
    # impossible in every supported database.
    Column("message_sid", String(64), primary_key=True),
    Column("provider", String(24), nullable=False),
    Column("account_sid", String(64), nullable=False),
    Column("tenant_id", Integer, nullable=False),
    Column("stream_key", String(64), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("encryption_key_id", String(48), nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("envelope_hmac", String(64), nullable=False),
    Column("status", String(20), nullable=False, server_default="buffered"),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="12"),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_token_hmac", String(64), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("replayed_at", DateTime(timezone=True), nullable=True),
    Column("target_turn_id", String(36), nullable=True),
    Column("target_outcome", String(24), nullable=True),
    Column("last_error_code", String(96), nullable=True),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("contract_version", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('buffered', 'replaying', 'retry_wait', 'replayed', 'dead')",
        name="ck_cutover_whatsapp_ingress_status",
    ),
    CheckConstraint(
        "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
        name="ck_cutover_whatsapp_ingress_attempts",
    ),
    CheckConstraint(
        "length(payload_digest) = 64 AND length(envelope_hmac) = 64",
        name="ck_cutover_whatsapp_ingress_digests",
    ),
    CheckConstraint(
        "((status = 'replaying' AND lease_token_hmac IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status <> 'replaying' AND lease_token_hmac IS NULL "
        "AND lease_expires_at IS NULL))",
        name="ck_cutover_whatsapp_ingress_lease",
    ),
    CheckConstraint(
        "((status = 'replayed' AND replayed_at IS NOT NULL "
        "AND target_turn_id IS NOT NULL AND target_outcome IS NOT NULL) OR "
        "status <> 'replayed')",
        name="ck_cutover_whatsapp_ingress_replayed",
    ),
)

Index(
    "ix_cutover_whatsapp_ingress_due",
    buffered_whatsapp_ingress.c.status,
    buffered_whatsapp_ingress.c.available_at,
    buffered_whatsapp_ingress.c.received_at,
    buffered_whatsapp_ingress.c.message_sid,
)
Index(
    "ix_cutover_whatsapp_ingress_stream_fifo",
    buffered_whatsapp_ingress.c.tenant_id,
    buffered_whatsapp_ingress.c.stream_key,
    buffered_whatsapp_ingress.c.received_at,
    buffered_whatsapp_ingress.c.message_sid,
)
Index(
    "uq_cutover_whatsapp_ingress_stream_replaying",
    buffered_whatsapp_ingress.c.tenant_id,
    buffered_whatsapp_ingress.c.stream_key,
    unique=True,
    postgresql_where=text("status = 'replaying'"),
    sqlite_where=text("status = 'replaying'"),
)
