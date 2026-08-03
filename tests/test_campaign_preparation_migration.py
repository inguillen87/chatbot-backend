from __future__ import annotations

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
    / "20260802_add_campaign_preparation_queue_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "campaign_preparation_queue_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_campaign(connection, *, campaign_id: str, tenant_id: int, idem: str):
    connection.execute(
        sa.text(
            "INSERT INTO campaign_preparation "
            "(id, tenant_id, created_by_user_id, contract_version, status, channel, "
            "template_id, template_key, idempotency_key, payload_digest, "
            "preview_digest, rendered_body, audience_json, readiness_json) VALUES "
            "(:id, :tenant_id, :user_id, 'crm_campaign_prepare.v1', 'draft', "
            "'whatsapp', :template_id, 'notice', :idem, :payload_digest, "
            ":preview_digest, 'body', '{}', '{}')"
        ),
        {
            "id": campaign_id,
            "tenant_id": tenant_id,
            "user_id": tenant_id,
            "template_id": f"template-{tenant_id}",
            "idem": idem,
            "payload_digest": "a" * 64,
            "preview_digest": "b" * 64,
        },
    )


def _insert_intent(
    connection,
    *,
    intent_id: str,
    campaign_id: str,
    tenant_id: int,
    contact_id: str,
    queue_status: str = "held",
    exclusion_reason: str | None = None,
    transport_status: str = "not_attempted",
    destination_digest: str | None = None,
    attempt_count: int = 0,
    provider_message_id: str | None = None,
):
    connection.execute(
        sa.text(
            "INSERT INTO campaign_delivery_intent "
            "(id, campaign_id, tenant_id, contact_id, queue_status, "
            "exclusion_reason, transport_status, destination_digest, "
            "rendered_content_digest, attempt_count, provider_message_id) VALUES "
            "(:id, :campaign_id, :tenant_id, :contact_id, :queue_status, "
            ":exclusion_reason, :transport_status, :destination_digest, "
            ":rendered_digest, :attempt_count, :provider_message_id)"
        ),
        {
            "id": intent_id,
            "campaign_id": campaign_id,
            "tenant_id": tenant_id,
            "contact_id": contact_id,
            "queue_status": queue_status,
            "exclusion_reason": exclusion_reason,
            "transport_status": transport_status,
            "destination_digest": destination_digest,
            "rendered_digest": "c" * 64,
            "attempt_count": attempt_count,
            "provider_message_id": provider_message_id,
        },
    )


def test_campaign_preparation_migration_enforces_prepare_only_receipts(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'campaign-preparation.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "notification_template",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "message_template_registry",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table("contact", metadata, sa.Column("id", sa.String(36), primary_key=True))
    metadata.create_all(engine)
    migration = _load_migration()

    assert migration.revision == "20260802_campaign_prepare_v1"
    assert migration.down_revision == "20260802_notification_wa_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1), (2)'))
        connection.execute(
            sa.text(
                "INSERT INTO notification_template (id) VALUES "
                "('template-1'), ('template-2')"
            )
        )
        connection.execute(
            sa.text("INSERT INTO contact (id) VALUES ('contact-1'), ('contact-2')")
        )

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert {
            "campaign_preparation",
            "campaign_delivery_intent",
        }.issubset(inspector.get_table_names())
        campaign_columns = {
            column["name"]
            for column in inspector.get_columns("campaign_preparation")
        }
        assert {
            "tenant_id",
            "idempotency_key",
            "payload_digest",
            "preview_digest",
            "audience_json",
            "readiness_json",
        }.issubset(campaign_columns)
        intent_columns = {
            column["name"]
            for column in inspector.get_columns("campaign_delivery_intent")
        }
        assert {
            "queue_status",
            "transport_status",
            "destination_digest",
            "rendered_content_digest",
            "attempt_count",
            "provider_message_id",
        }.issubset(intent_columns)
        assert not {
            "phone",
            "email",
            "recipient",
            "destination",
            "message_body",
        }.intersection(intent_columns)

        _insert_campaign(
            connection, campaign_id="campaign-1", tenant_id=1, idem="idem-shared"
        )
        _insert_intent(
            connection,
            intent_id="intent-1",
            campaign_id="campaign-1",
            tenant_id=1,
            contact_id="contact-1",
            destination_digest="d" * 64,
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_campaign(
                    connection,
                    campaign_id="campaign-duplicate",
                    tenant_id=1,
                    idem="idem-shared",
                )
        _insert_campaign(
            connection, campaign_id="campaign-2", tenant_id=2, idem="idem-shared"
        )

        invalid_intents = [
            {
                "intent_id": "intent-no-destination",
                "queue_status": "held",
                "destination_digest": None,
            },
            {
                "intent_id": "intent-no-reason",
                "queue_status": "excluded",
                "destination_digest": None,
                "exclusion_reason": None,
            },
            {
                "intent_id": "intent-false-delivery",
                "queue_status": "held",
                "destination_digest": "d" * 64,
                "transport_status": "delivered",
            },
            {
                "intent_id": "intent-ambiguous-io",
                "queue_status": "held",
                "destination_digest": "d" * 64,
                "transport_status": "unknown",
                "attempt_count": 1,
            },
        ]
        for invalid in invalid_intents:
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    _insert_intent(
                        connection,
                        campaign_id="campaign-1",
                        tenant_id=1,
                        contact_id="contact-2",
                        **invalid,
                    )

        with pytest.raises(RuntimeError, match="erase queue receipts"):
            with Operations.context(context):
                migration.downgrade()

        connection.execute(sa.text("DELETE FROM campaign_delivery_intent"))
        connection.execute(sa.text("DELETE FROM campaign_preparation"))
        with Operations.context(context):
            migration.downgrade()
        assert "campaign_preparation" not in sa.inspect(connection).get_table_names()
        assert "campaign_delivery_intent" not in sa.inspect(connection).get_table_names()

    engine.dispose()
