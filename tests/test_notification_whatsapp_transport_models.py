from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from app import db
from models import (
    MessageTemplateRegistry,
    MessagingEventLedger,
    Notification,
    NotificationAttempt,
    NotificationTemplate,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    User,
)


def _constraint_columns(constraint) -> tuple[str, ...]:
    return tuple(column.name for column in constraint.columns)


def test_notification_transport_model_metadata_is_explicit_and_fail_closed():
    assert Notification.VALID_STATUSES == {
        "queued",
        "delayed",
        "sending",
        "retry_wait",
        "sent",
        "send_uncertain",
        "failed",
        "blocked",
    }
    assert NotificationAttempt.VALID_STATUSES == {
        "success",
        "failed",
        "delayed",
        "blocked",
        "sending",
        "send_uncertain",
    }

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
    } <= set(Notification.__table__.columns.keys())
    assert {
        "provider_status",
        "error_digest",
        "delivery_event_id",
    } <= set(NotificationAttempt.__table__.columns.keys())
    assert (
        NotificationTemplate.__table__.c.message_template_registry_id.nullable
        is True
    )

    assert Notification.__table__.c.provider_message_id.type.length == 180
    assert NotificationAttempt.__table__.c.provider_message_id.type.length == 180
    assert Notification.__table__.c.provider_status.nullable is False
    assert NotificationAttempt.__table__.c.provider_status.nullable is False

    notification_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Notification.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_notification_status",
        "ck_notification_provider_status",
        "ck_notification_sender_binding_length",
        "ck_notification_payload_digest_length",
        "ck_notification_lease_state",
    } <= set(notification_checks)
    assert "blocked" in notification_checks["ck_notification_status"]
    assert "send_uncertain" in notification_checks["ck_notification_status"]

    attempt_constraints = NotificationAttempt.__table__.constraints
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_notification_attempt_number"
        and _constraint_columns(constraint)
        == ("notification_id", "attempt_number")
        for constraint in attempt_constraints
    )
    assert any(
        isinstance(index, Index)
        and index.name == "ix_notification_tenant_due"
        and tuple(expression.name for expression in index.expressions)
        == ("tenant_id", "status", "next_retry_at", "id")
        for index in Notification.__table__.indexes
    )

    expected_foreign_keys = {
        "message_template_registry_id": (
            "message_template_registry.id",
            "SET NULL",
        ),
        "provider_connection_id": ("provider_connection.id", "SET NULL"),
        "provider_sender_id": ("provider_sender.id", "SET NULL"),
    }
    for column_name, (target, ondelete) in expected_foreign_keys.items():
        foreign_key = next(iter(Notification.__table__.c[column_name].foreign_keys))
        assert foreign_key.target_fullname == target
        assert foreign_key.ondelete == ondelete
    delivery_event_fk = next(
        iter(NotificationAttempt.__table__.c.delivery_event_id.foreign_keys)
    )
    assert delivery_event_fk.target_fullname == "messaging_event_ledger.id"
    assert delivery_event_fk.ondelete == "SET NULL"


def test_notification_transport_models_persist_snapshots_leases_and_blocked_state(
    client,
):
    owner = User(
        email="transport-owner@test.com",
        name="Transport Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="transport-models",
        nombre="Transport Models",
        tipo="municipio",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id

    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="claim_created_v1",
        language="es_AR",
        category="utility",
        status="approved",
        content_sid="HXclaim-created-v1",
    )
    provider_connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="active",
        display_name="Municipio Twilio",
    )
    db.session.add_all([registry, provider_connection])
    db.session.flush()

    provider_sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=provider_connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number="+5492600000000",
        sender_sid="MGsender-v1",
        status="active",
    )
    event = MessagingEventLedger(
        tenant_id=tenant.id,
        provider_connection_id=provider_connection.id,
        channel="whatsapp",
        direction="outbound",
        event_type="provider.accepted",
        provider="twilio",
        provider_event_id="callback-1",
    )
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="claim_created",
        channel="whatsapp",
        body_template="Tu reclamo ${claim_id} fue creado",
        message_template_registry_id=registry.id,
    )
    db.session.add_all([provider_sender, event, template])
    db.session.flush()
    event.provider_sender_id = provider_sender.id

    lease_deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    notification = Notification(
        tenant_id=tenant.id,
        user_id=owner.id,
        template_id=template.id,
        channel="whatsapp",
        recipient="whatsapp:+5492612345678",
        body="Tu reclamo 401746 fue creado",
        status="sending",
        idempotency_key="claim-created:401746",
        max_retries=3,
        attempt_count=1,
        message_template_registry_id=registry.id,
        provider_connection_id=provider_connection.id,
        provider_sender_id=provider_sender.id,
        sender_binding="a" * 64,
        content_sid="HXclaim-created-v1",
        content_variables={"1": "Marcelo", "2": "401746"},
        payload_digest="b" * 64,
        provider_message_id="SMaccepted-1",
        provider_status="accepted",
        lease_token="lease-token-1",
        leased_until=lease_deadline,
    )
    blocked = Notification(
        tenant_id=tenant.id,
        user_id=owner.id,
        channel="whatsapp",
        recipient="whatsapp:+5492687654321",
        body="Pendiente de configuración",
        status="blocked",
        idempotency_key="blocked:1",
        max_retries=3,
        attempt_count=1,
        last_error="whatsapp_transport_unavailable",
    )
    db.session.add_all([notification, blocked])
    db.session.flush()

    attempt = NotificationAttempt(
        notification_id=notification.id,
        tenant_id=tenant.id,
        attempt_number=1,
        status="send_uncertain",
        provider="twilio",
        provider_message_id="SMaccepted-1",
        provider_status="accepted",
        error_digest="c" * 64,
        delivery_event_id=event.id,
    )
    blocked_attempt = NotificationAttempt(
        notification_id=blocked.id,
        tenant_id=tenant.id,
        attempt_number=1,
        status="blocked",
        provider="whatsapp",
        provider_status="unknown",
        error_message="whatsapp_transport_unavailable",
    )
    db.session.add_all([attempt, blocked_attempt])
    db.session.commit()

    persisted = db.session.get(Notification, notification.id)
    persisted_attempt = db.session.get(NotificationAttempt, attempt.id)
    assert persisted is not None
    assert persisted.content_variables == {"1": "Marcelo", "2": "401746"}
    assert persisted.message_template_registry is registry
    assert persisted.provider_connection is provider_connection
    assert persisted.provider_sender is provider_sender
    assert persisted.lease_token == "lease-token-1"
    assert persisted_attempt is not None
    assert persisted_attempt.delivery_event is event
    assert db.session.get(Notification, blocked.id).status == "blocked"
    assert db.session.get(NotificationAttempt, blocked_attempt.id).status == "blocked"

    db.session.add(
        NotificationAttempt(
            notification_id=notification.id,
            tenant_id=tenant.id,
            attempt_number=1,
            status="failed",
            provider_status="failed",
        )
    )
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    db.session.add(
        Notification(
            tenant_id=tenant.id,
            channel="whatsapp",
            recipient="whatsapp:+5492600000001",
            body="Lease faltante",
            status="sending",
            idempotency_key="sending-without-lease",
        )
    )
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
