"""add provider platform tables

Revision ID: 20260521_provider_platform
Revises: c4f5d8e9b123
Create Date: 2026-05-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260521_provider_platform"
down_revision = "c4f5d8e9b123"
branch_labels = None
depends_on = None


def _json_type():
    bind = op.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade():
    json_type = _json_type()

    op.create_table(
        "provider_connection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("environment", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("external_account_id", sa.String(length=120), nullable=True),
        sa.Column("external_business_id", sa.String(length=120), nullable=True),
        sa.Column("external_app_id", sa.String(length=120), nullable=True),
        sa.Column("configuration_id", sa.String(length=120), nullable=True),
        sa.Column("partner_solution_id", sa.String(length=120), nullable=True),
        sa.Column("credentials_ref", sa.String(length=255), nullable=True),
        sa.Column("capabilities", json_type, nullable=True),
        sa.Column("config", json_type, nullable=True),
        sa.Column("health", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", "channel", "environment", name="uq_provider_connection_tenant_provider_channel_env"),
    )
    op.create_index(op.f("ix_provider_connection_tenant_id"), "provider_connection", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_connection_provider"), "provider_connection", ["provider"], unique=False)
    op.create_index(op.f("ix_provider_connection_channel"), "provider_connection", ["channel"], unique=False)
    op.create_index(op.f("ix_provider_connection_environment"), "provider_connection", ["environment"], unique=False)
    op.create_index(op.f("ix_provider_connection_status"), "provider_connection", ["status"], unique=False)
    op.create_index(op.f("ix_provider_connection_external_account_id"), "provider_connection", ["external_account_id"], unique=False)
    op.create_index(op.f("ix_provider_connection_external_business_id"), "provider_connection", ["external_business_id"], unique=False)
    op.create_index(op.f("ix_provider_connection_external_app_id"), "provider_connection", ["external_app_id"], unique=False)

    op.create_table(
        "provider_sender",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("sender_type", sa.String(length=50), nullable=False),
        sa.Column("phone_number", sa.String(length=50), nullable=True),
        sa.Column("sender_id", sa.String(length=255), nullable=True),
        sa.Column("sender_sid", sa.String(length=120), nullable=True),
        sa.Column("messaging_service_sid", sa.String(length=120), nullable=True),
        sa.Column("waba_id", sa.String(length=120), nullable=True),
        sa.Column("phone_number_id", sa.String(length=120), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("verification_status", sa.String(length=50), nullable=True),
        sa.Column("quality_rating", sa.String(length=50), nullable=True),
        sa.Column("webhook_url", sa.String(length=500), nullable=True),
        sa.Column("status_callback_url", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", json_type, nullable=True),
        sa.Column("last_status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connection.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "channel", "phone_number", name="uq_provider_sender_tenant_channel_phone"),
    )
    op.create_index("ix_provider_sender_tenant_status", "provider_sender", ["tenant_id", "status"], unique=False)
    op.create_index(op.f("ix_provider_sender_tenant_id"), "provider_sender", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_sender_provider_connection_id"), "provider_sender", ["provider_connection_id"], unique=False)
    op.create_index(op.f("ix_provider_sender_channel"), "provider_sender", ["channel"], unique=False)
    op.create_index(op.f("ix_provider_sender_phone_number"), "provider_sender", ["phone_number"], unique=False)
    op.create_index(op.f("ix_provider_sender_sender_id"), "provider_sender", ["sender_id"], unique=False)
    op.create_index(op.f("ix_provider_sender_sender_sid"), "provider_sender", ["sender_sid"], unique=False)
    op.create_index(op.f("ix_provider_sender_messaging_service_sid"), "provider_sender", ["messaging_service_sid"], unique=False)
    op.create_index(op.f("ix_provider_sender_waba_id"), "provider_sender", ["waba_id"], unique=False)
    op.create_index(op.f("ix_provider_sender_phone_number_id"), "provider_sender", ["phone_number_id"], unique=False)
    op.create_index(op.f("ix_provider_sender_status"), "provider_sender", ["status"], unique=False)

    op.create_table(
        "messaging_event_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider_connection_id", sa.Integer(), nullable=True),
        sa.Column("provider_sender_id", sa.Integer(), nullable=True),
        sa.Column("contact_id", sa.String(length=36), nullable=True),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_event_id", sa.String(length=180), nullable=True),
        sa.Column("external_message_sid", sa.String(length=180), nullable=True),
        sa.Column("external_status", sa.String(length=80), nullable=True),
        sa.Column("sender", sa.String(length=255), nullable=True),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("payload", json_type, nullable=True),
        sa.Column("metadata_json", json_type, nullable=True),
        sa.Column("request_id", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_connection_id"], ["provider_connection.id"]),
        sa.ForeignKeyConstraint(["provider_sender_id"], ["provider_sender.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messaging_event_tenant_channel_at", "messaging_event_ledger", ["tenant_id", "channel", "occurred_at"], unique=False)
    op.create_index("ix_messaging_event_tenant_status", "messaging_event_ledger", ["tenant_id", "external_status"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_tenant_id"), "messaging_event_ledger", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_provider_connection_id"), "messaging_event_ledger", ["provider_connection_id"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_provider_sender_id"), "messaging_event_ledger", ["provider_sender_id"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_contact_id"), "messaging_event_ledger", ["contact_id"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_channel"), "messaging_event_ledger", ["channel"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_direction"), "messaging_event_ledger", ["direction"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_event_type"), "messaging_event_ledger", ["event_type"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_provider"), "messaging_event_ledger", ["provider"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_provider_event_id"), "messaging_event_ledger", ["provider_event_id"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_external_message_sid"), "messaging_event_ledger", ["external_message_sid"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_external_status"), "messaging_event_ledger", ["external_status"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_recipient"), "messaging_event_ledger", ["recipient"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_request_id"), "messaging_event_ledger", ["request_id"], unique=False)
    op.create_index(op.f("ix_messaging_event_ledger_occurred_at"), "messaging_event_ledger", ["occurred_at"], unique=False)

    op.create_table(
        "consent_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("contact_key", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("evidence_payload", json_type, nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_consent_ledger_tenant_id"), "consent_ledger", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_consent_ledger_contact_key"), "consent_ledger", ["contact_key"], unique=False)
    op.create_index(op.f("ix_consent_ledger_channel"), "consent_ledger", ["channel"], unique=False)
    op.create_index(op.f("ix_consent_ledger_status"), "consent_ledger", ["status"], unique=False)
    op.create_index(op.f("ix_consent_ledger_effective_at"), "consent_ledger", ["effective_at"], unique=False)

    op.create_table(
        "message_template_registry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("content_sid", sa.String(length=120), nullable=True),
        sa.Column("external_template_id", sa.String(length=120), nullable=True),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("components", json_type, nullable=True),
        sa.Column("metadata_json", json_type, nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", "channel", "name", "language", name="uq_template_registry_identity"),
    )
    op.create_index(op.f("ix_message_template_registry_tenant_id"), "message_template_registry", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_message_template_registry_provider"), "message_template_registry", ["provider"], unique=False)
    op.create_index(op.f("ix_message_template_registry_channel"), "message_template_registry", ["channel"], unique=False)
    op.create_index(op.f("ix_message_template_registry_category"), "message_template_registry", ["category"], unique=False)
    op.create_index(op.f("ix_message_template_registry_status"), "message_template_registry", ["status"], unique=False)
    op.create_index(op.f("ix_message_template_registry_content_sid"), "message_template_registry", ["content_sid"], unique=False)
    op.create_index(op.f("ix_message_template_registry_external_template_id"), "message_template_registry", ["external_template_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_message_template_registry_external_template_id"), table_name="message_template_registry")
    op.drop_index(op.f("ix_message_template_registry_content_sid"), table_name="message_template_registry")
    op.drop_index(op.f("ix_message_template_registry_status"), table_name="message_template_registry")
    op.drop_index(op.f("ix_message_template_registry_category"), table_name="message_template_registry")
    op.drop_index(op.f("ix_message_template_registry_channel"), table_name="message_template_registry")
    op.drop_index(op.f("ix_message_template_registry_provider"), table_name="message_template_registry")
    op.drop_index(op.f("ix_message_template_registry_tenant_id"), table_name="message_template_registry")
    op.drop_table("message_template_registry")

    op.drop_index(op.f("ix_consent_ledger_effective_at"), table_name="consent_ledger")
    op.drop_index(op.f("ix_consent_ledger_status"), table_name="consent_ledger")
    op.drop_index(op.f("ix_consent_ledger_channel"), table_name="consent_ledger")
    op.drop_index(op.f("ix_consent_ledger_contact_key"), table_name="consent_ledger")
    op.drop_index(op.f("ix_consent_ledger_tenant_id"), table_name="consent_ledger")
    op.drop_table("consent_ledger")

    op.drop_index(op.f("ix_messaging_event_ledger_occurred_at"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_request_id"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_recipient"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_external_status"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_external_message_sid"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_provider_event_id"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_provider"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_event_type"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_direction"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_channel"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_contact_id"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_provider_sender_id"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_provider_connection_id"), table_name="messaging_event_ledger")
    op.drop_index(op.f("ix_messaging_event_ledger_tenant_id"), table_name="messaging_event_ledger")
    op.drop_index("ix_messaging_event_tenant_status", table_name="messaging_event_ledger")
    op.drop_index("ix_messaging_event_tenant_channel_at", table_name="messaging_event_ledger")
    op.drop_table("messaging_event_ledger")

    op.drop_index(op.f("ix_provider_sender_status"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_phone_number_id"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_waba_id"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_messaging_service_sid"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_sender_sid"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_sender_id"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_phone_number"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_channel"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_provider_connection_id"), table_name="provider_sender")
    op.drop_index(op.f("ix_provider_sender_tenant_id"), table_name="provider_sender")
    op.drop_index("ix_provider_sender_tenant_status", table_name="provider_sender")
    op.drop_table("provider_sender")

    op.drop_index(op.f("ix_provider_connection_external_app_id"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_external_business_id"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_external_account_id"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_status"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_environment"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_channel"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_provider"), table_name="provider_connection")
    op.drop_index(op.f("ix_provider_connection_tenant_id"), table_name="provider_connection")
    op.drop_table("provider_connection")
