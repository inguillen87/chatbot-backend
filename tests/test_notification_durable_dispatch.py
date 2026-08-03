from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from models import (
    MessageTemplateRegistry,
    MessagingEventLedger,
    Notification,
    NotificationAttempt,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    WhatsAppEnterpriseRule,
    db,
)
from services.notification_orchestrator import (
    NotificationOrchestrator,
    reconcile_whatsapp_notification_status,
)


def _seed_transport(app, monkeypatch, owner_user, *, slug: str):
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    account_sid = f"AC{tenant.id:032x}"
    token_ref = f"TWILIO_NOTIFICATION_TOKEN_{tenant.id}"
    monkeypatch.setitem(app.config, token_ref, f"token-{tenant.id}")
    tenant.configuracion = {
        "twilio_tech_provider": {
            "twilio_account_sid": account_sid,
            "twilio_subaccount_token_ref": token_ref,
        }
    }
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id=account_sid,
        credentials_ref=f"env:{token_ref}",
    )
    db.session.add(connection)
    db.session.flush()
    phone = f"+1500555{tenant.id:04d}"
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=phone,
        sender_id=f"whatsapp:{phone}",
        status="online",
        status_callback_url="https://api.example.test/twilio/whatsapp/status",
    )
    db.session.add(sender)
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"claim-confirmed-{tenant.id}",
        language="es_AR",
        status="approved",
        content_sid="HX" + f"{tenant.id:x}".rjust(32, "a")[-32:],
        body_preview="Registramos el reclamo {{1}}.",
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry)
    db.session.commit()
    monkeypatch.setitem(app.config, "WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED", True)
    monkeypatch.setitem(
        app.config,
        "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS",
        str(tenant.id),
    )
    return tenant, sender, registry


def _queue(tenant, registry, *, key: str, max_retries: int = 3):
    notification, created = NotificationOrchestrator(tenant.id).queue_notification(
        channel="whatsapp",
        recipient="+5492613333333",
        idempotency_key=key,
        template_registry_id=registry.id,
        content_variables={"1": "REC-100"},
        max_retries=max_retries,
        metadata={
            "content_sid": "HXattacker",
            "provider_sender_id": 999999,
            "status_callback": "https://attacker.example/callback",
            "within_24h_window": True,
        },
    )
    db.session.commit()
    assert created is True
    return notification


def test_durable_template_dispatch_claims_once_and_persists_provider_ack(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-success",
    )
    notification = _queue(tenant, registry, key="notification-success-1")
    calls = []

    def _provider_send(prepared, *, on_provider_call_start=None):
        calls.append(dict(prepared.params))
        assert callable(on_provider_call_start)
        on_provider_call_start()
        return "SMnotificationaccepted001"

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_provider_send,
    ):
        first = NotificationOrchestrator(tenant.id).dispatch_due_notifications()
        second = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    assert first["sent"] == 1
    assert first["send_uncertain"] == 0
    assert second["processed"] == 0
    assert len(calls) == 1
    assert calls[0]["content_sid"] == registry.content_sid
    assert calls[0]["from_"] == sender.sender_id
    assert "body" not in calls[0]
    assert "attacker.example" not in calls[0]["status_callback"]
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_SENT
    assert persisted.provider_status == "accepted"
    assert persisted.provider_message_id == "SMnotificationaccepted001"
    assert persisted.lease_token is None
    attempt = NotificationAttempt.query.filter_by(notification_id=persisted.id).one()
    assert attempt.status == NotificationAttempt.STATUS_SUCCESS
    assert attempt.provider_message_id == persisted.provider_message_id


def test_fast_callback_before_provider_return_is_idempotent_and_never_regresses(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-fast-callback",
    )
    notification = _queue(tenant, registry, key="notification-fast-callback-1")

    def _provider_send(prepared, *, on_provider_call_start=None):
        on_provider_call_start()
        attempt_id = parse_qs(
            urlparse(prepared.params["status_callback"]).query
        )["notification_attempt_id"][0]
        assert reconcile_whatsapp_notification_status(
            tenant_id=tenant.id,
            notification_attempt_id=attempt_id,
            provider_message_sid="SMfastcallback001",
            provider_status="delivered",
            provider_sender_id=sender.id,
        )
        return "SMfastcallback001"

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_provider_send,
    ):
        summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    assert summary["sent"] == 1
    assert summary["failed"] == 0
    assert summary["send_uncertain"] == 0
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_SENT
    assert persisted.provider_status == "delivered"
    assert persisted.provider_message_id == "SMfastcallback001"


def test_fast_callback_before_provider_timeout_wins_over_uncertain_fallback(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-fast-timeout-callback",
    )
    notification = _queue(
        tenant,
        registry,
        key="notification-fast-timeout-callback-1",
    )

    def _provider_timeout(prepared, *, on_provider_call_start=None):
        on_provider_call_start()
        attempt_id = parse_qs(
            urlparse(prepared.params["status_callback"]).query
        )["notification_attempt_id"][0]
        assert reconcile_whatsapp_notification_status(
            tenant_id=tenant.id,
            notification_attempt_id=attempt_id,
            provider_message_sid="SMfasttimeoutcallback001",
            provider_status="delivered",
            provider_sender_id=sender.id,
        )
        raise TimeoutError("provider response arrived after signed callback")

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_provider_timeout,
    ):
        summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    assert summary["sent"] == 1
    assert summary["send_uncertain"] == 0
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_SENT
    assert persisted.provider_status == "delivered"


def test_timeout_after_provider_hook_is_quarantined_and_never_auto_retried(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-uncertain",
    )
    notification = _queue(tenant, registry, key="notification-uncertain-1")
    calls = []

    def _timeout(_prepared, *, on_provider_call_start=None):
        calls.append("called")
        on_provider_call_start()
        raise TimeoutError("private provider timeout payload")

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_timeout,
    ):
        first = NotificationOrchestrator(tenant.id).dispatch_due_notifications()
        second = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    assert first["send_uncertain"] == 1
    assert second["processed"] == 0
    assert calls == ["called"]
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_SEND_UNCERTAIN
    assert persisted.next_retry_at is None
    assert "private provider timeout payload" not in (persisted.last_error or "")
    assert NotificationOrchestrator(tenant.id).requeue_blocked_notifications() == 0


def test_provider_network_failure_before_hook_is_blocked_without_fake_send(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-pre-io",
    )
    notification = _queue(tenant, registry, key="notification-pre-io-1")

    class ProviderNetworkDisabledError(RuntimeError):
        pass

    def _blocked(_prepared, *, on_provider_call_start=None):
        assert callable(on_provider_call_start)
        raise ProviderNetworkDisabledError("network disabled")

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_blocked,
    ):
        summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    assert summary["blocked"] == 1
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_BLOCKED
    assert persisted.last_error == "twilio_provider_network_disabled"
    assert persisted.provider_message_id is None


def test_expired_post_io_lease_becomes_uncertain_without_provider_call(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-expired",
    )
    notification = _queue(tenant, registry, key="notification-expired-1")
    notification.status = Notification.STATUS_SENDING
    notification.attempt_count = 1
    notification.lease_token = "expired-lease"
    notification.leased_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    attempt = NotificationAttempt(
        notification_id=notification.id,
        tenant_id=tenant.id,
        attempt_number=1,
        status=NotificationAttempt.STATUS_SENDING,
        provider="whatsapp",
        metadata_json={"provider_call_started": True},
    )
    db.session.add(attempt)
    db.session.commit()

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message"
    ) as provider_send:
        summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    assert summary["processed"] == 0
    provider_send.assert_not_called()
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_SEND_UNCERTAIN
    assert persisted.next_retry_at is None
    persisted_attempt = db.session.get(NotificationAttempt, attempt.id)
    assert persisted_attempt.status == NotificationAttempt.STATUS_SEND_UNCERTAIN


def test_signed_callback_reconciliation_is_tenant_sender_sid_and_order_bound(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-callback",
    )
    notification = _queue(tenant, registry, key="notification-callback-1")

    def _timeout(_prepared, *, on_provider_call_start=None):
        on_provider_call_start()
        raise TimeoutError("ambiguous")

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_timeout,
    ):
        NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    db.session.expire_all()
    attempt = NotificationAttempt.query.filter_by(notification_id=notification.id).one()
    assert reconcile_whatsapp_notification_status(
        tenant_id=tenant.id,
        notification_attempt_id=attempt.id,
        provider_message_sid="SMcallback001",
        provider_status="delivered",
        provider_sender_id=sender.id,
    )
    # A late failure cannot regress a delivered message.
    assert reconcile_whatsapp_notification_status(
        tenant_id=tenant.id,
        notification_attempt_id=attempt.id,
        provider_message_sid="SMcallback001",
        provider_status="failed",
        provider_sender_id=sender.id,
    )
    assert not reconcile_whatsapp_notification_status(
        tenant_id=tenant.id,
        notification_attempt_id=attempt.id,
        provider_message_sid="SMdifferent",
        provider_status="read",
        provider_sender_id=sender.id,
    )
    assert not reconcile_whatsapp_notification_status(
        tenant_id=tenant.id,
        notification_attempt_id=attempt.id,
        provider_message_sid="SMcallback001",
        provider_status="read",
        provider_sender_id=sender.id + 9999,
    )
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.status == Notification.STATUS_SENT
    assert persisted.provider_status == "delivered"
    assert persisted.provider_message_id == "SMcallback001"


def test_enqueue_rejects_stale_or_cross_tenant_registry(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    first, _sender, first_registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-registry-first",
    )
    second, _other_sender, second_registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-registry-second",
    )
    first_registry.last_sync_at = datetime.now(timezone.utc) - timedelta(days=8)
    db.session.commit()

    with pytest.raises(ValueError, match="whatsapp_template_not_approved"):
        _queue(first, first_registry, key="notification-stale-1")
    with pytest.raises(ValueError, match="whatsapp_template_registry_mismatch"):
        _queue(first, second_registry, key="notification-cross-tenant-1")


def test_durable_notification_reservations_enforce_hourly_limit_without_double_count(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-rate-limit",
    )
    db.session.add(
        WhatsAppEnterpriseRule(tenant_id=tenant.id, max_outbound_per_hour=1)
    )
    db.session.commit()
    first = _queue(tenant, registry, key="notification-rate-first")
    second = _queue(tenant, registry, key="notification-rate-second")
    calls = []

    def _accepted(_prepared, *, on_provider_call_start=None):
        calls.append("provider")
        on_provider_call_start()
        return "SMratelimitaccepted001"

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        side_effect=_accepted,
    ):
        first_summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications(
            limit=1
        )
        second_summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications(
            limit=1
        )

    assert first_summary["sent"] == 1
    assert second_summary["failed"] == 1
    assert calls == ["provider"]
    db.session.expire_all()
    persisted = [
        db.session.get(Notification, first.id),
        db.session.get(Notification, second.id),
    ]
    sent = [row for row in persisted if row.status == Notification.STATUS_SENT]
    limited_rows = [row for row in persisted if row.last_error == "rate_limited"]
    assert len(sent) == 1
    assert len(limited_rows) == 1
    limited = limited_rows[0]
    assert limited.last_error == "rate_limited"
    assert limited.provider_message_id is None


def test_local_preflight_failure_does_not_consume_hourly_provider_quota(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender, registry = _seed_transport(
        app,
        monkeypatch,
        owner_user,
        slug="notification-preflight-quota",
    )
    sender.status_callback_url = None
    db.session.add(
        WhatsAppEnterpriseRule(tenant_id=tenant.id, max_outbound_per_hour=1)
    )
    db.session.commit()
    notification = _queue(
        tenant,
        registry,
        key="notification-preflight-quota-1",
    )

    with patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message"
    ) as provider_send:
        summary = NotificationOrchestrator(tenant.id).dispatch_due_notifications()

    provider_send.assert_not_called()
    assert summary["blocked"] == 1
    db.session.expire_all()
    persisted = db.session.get(Notification, notification.id)
    assert persisted.last_error == "whatsapp_status_callback_missing"
    reservations = MessagingEventLedger.query.filter_by(
        tenant_id=tenant.id,
        channel="whatsapp",
        event_type="whatsapp_outbound_rate_limit_reserved",
    ).count()
    assert reservations == 0
