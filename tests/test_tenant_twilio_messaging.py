from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse
from unittest.mock import MagicMock, patch

import pytest

from models import (
    MessageTemplateRegistry,
    Notification,
    NotificationAttempt,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    db,
)
from services.tenant_twilio_messaging import (
    PreparedTenantTwilioMessage,
    build_tenant_twilio_sender_binding,
    prepare_bound_tenant_twilio_message,
    send_prepared_tenant_twilio_message,
)


def _seed_twilio_tenant(
    *,
    app,
    monkeypatch,
    owner_user,
    slug: str,
) -> tuple[TenantProfile, ProviderSender]:
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
    token_ref = f"TWILIO_SUBACCOUNT_AUTH_TOKEN_{slug.upper().replace('-', '_')}"
    monkeypatch.setitem(app.config, token_ref, f"tenant-token-{tenant.id}")
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
        status_callback_url=(
            "https://api.example.test/twilio/whatsapp/status?source=sender"
        ),
        metadata_json={
            "content_sid": "HX" + ("f" * 32),
            "status_callback": "https://attacker.example.test/callback",
        },
    )
    db.session.add(sender)
    db.session.flush()
    return tenant, sender


def _approved_template(tenant: TenantProfile, *, suffix: str) -> MessageTemplateRegistry:
    row = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"notification-{suffix}",
        language="es",
        status="approved",
        content_sid="HX" + (suffix[0].lower() * 32),
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(row)
    db.session.flush()
    return row


def _notification_attempt(
    tenant: TenantProfile,
    sender: ProviderSender,
    *,
    suffix: str,
) -> NotificationAttempt:
    notification = Notification(
        tenant_id=tenant.id,
        channel="whatsapp",
        recipient="+5492613333333",
        body="Vista local que no debe enviarse junto al ContentSid",
        status=Notification.STATUS_SENDING,
        idempotency_key=f"tenant-twilio-{suffix}",
        max_retries=3,
        attempt_count=1,
        provider_connection_id=sender.provider_connection_id,
        provider_sender_id=sender.id,
        lease_token=f"lease-{suffix}",
        leased_until=datetime.now(timezone.utc),
    )
    db.session.add(notification)
    db.session.flush()
    attempt = NotificationAttempt(
        notification_id=notification.id,
        tenant_id=tenant.id,
        attempt_number=1,
        status="sending",
        provider="whatsapp",
    )
    db.session.add(attempt)
    db.session.flush()
    return attempt


def test_prepares_approved_tenant_template_and_bound_attempt_callback(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender = _seed_twilio_tenant(
        app=app,
        monkeypatch=monkeypatch,
        owner_user=owner_user,
        slug="template-safe",
    )
    template = _approved_template(tenant, suffix="a-approved")
    attempt = _notification_attempt(tenant, sender, suffix="approved")
    binding = build_tenant_twilio_sender_binding(
        tenant_id=tenant.id,
        channel="whatsapp",
    )

    preflight = prepare_bound_tenant_twilio_message(
        tenant_id=tenant.id,
        channel="whatsapp",
        expected_sender_binding=binding,
        recipient="+5492613333333",
        template_registry_id=template.id,
        content_variables={"1": "Marcelo", "2": "T-1234"},
        notification_attempt_id=attempt.id,
    )

    assert preflight.reason_code is None
    assert preflight.prepared is not None
    params = dict(preflight.prepared.params)
    assert params["content_sid"] == template.content_sid
    assert json.loads(params["content_variables"]) == {
        "1": "Marcelo",
        "2": "T-1234",
    }
    assert params["to"] == "whatsapp:+5492613333333"
    assert params["from_"] == sender.sender_id
    assert "body" not in params
    assert "media_url" not in params
    callback = urlparse(params["status_callback"])
    assert (callback.scheme, callback.netloc, callback.path) == (
        "https",
        "api.example.test",
        "/twilio/whatsapp/status",
    )
    assert dict(parse_qsl(callback.query)) == {
        "source": "sender",
        "notification_attempt_id": attempt.id,
    }
    assert "attacker.example.test" not in params["status_callback"]
    assert params["content_sid"] != sender.metadata_json["content_sid"]


def test_rejects_cross_tenant_template_and_notification_attempt(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    first, first_sender = _seed_twilio_tenant(
        app=app,
        monkeypatch=monkeypatch,
        owner_user=owner_user,
        slug="scope-first",
    )
    second, other_sender = _seed_twilio_tenant(
        app=app,
        monkeypatch=monkeypatch,
        owner_user=owner_user,
        slug="scope-second",
    )
    first_template = _approved_template(first, suffix="b-first")
    other_template = _approved_template(second, suffix="c-second")
    first_attempt = _notification_attempt(first, first_sender, suffix="first")
    other_attempt = _notification_attempt(second, other_sender, suffix="second")
    binding = build_tenant_twilio_sender_binding(
        tenant_id=first.id,
        channel="whatsapp",
    )

    wrong_template = prepare_bound_tenant_twilio_message(
        tenant_id=first.id,
        channel="whatsapp",
        expected_sender_binding=binding,
        recipient="+5492613333333",
        template_registry_id=other_template.id,
        content_variables={"1": "seguro"},
        notification_attempt_id=first_attempt.id,
    )
    wrong_attempt = prepare_bound_tenant_twilio_message(
        tenant_id=first.id,
        channel="whatsapp",
        expected_sender_binding=binding,
        recipient="+5492613333333",
        template_registry_id=first_template.id,
        content_variables={"1": "seguro"},
        notification_attempt_id=other_attempt.id,
    )

    assert wrong_template.prepared is None
    assert wrong_template.reason_code == "whatsapp_template_registry_mismatch"
    assert wrong_attempt.prepared is None
    assert wrong_attempt.reason_code == "notification_attempt_scope_mismatch"


def test_rejects_attempt_bound_to_another_sender_in_the_same_tenant(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, sender = _seed_twilio_tenant(
        app=app,
        monkeypatch=monkeypatch,
        owner_user=owner_user,
        slug="attempt-sender-scope",
    )
    template = _approved_template(tenant, suffix="f-sender")
    attempt = _notification_attempt(tenant, sender, suffix="sender")
    notification = db.session.get(Notification, attempt.notification_id)
    notification.provider_sender_id = sender.id + 9999
    db.session.flush()
    binding = build_tenant_twilio_sender_binding(
        tenant_id=tenant.id,
        channel="whatsapp",
    )

    preflight = prepare_bound_tenant_twilio_message(
        tenant_id=tenant.id,
        channel="whatsapp",
        expected_sender_binding=binding,
        recipient="+5492613333333",
        template_registry_id=template.id,
        content_variables={"1": "seguro"},
        notification_attempt_id=attempt.id,
    )

    assert preflight.prepared is None
    assert preflight.reason_code == "notification_attempt_scope_mismatch"


def test_rejects_transport_shaped_variables_and_arbitrary_content_sid(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _sender = _seed_twilio_tenant(
        app=app,
        monkeypatch=monkeypatch,
        owner_user=owner_user,
        slug="unsafe-input",
    )
    template = _approved_template(tenant, suffix="d-unsafe")
    binding = build_tenant_twilio_sender_binding(
        tenant_id=tenant.id,
        channel="whatsapp",
    )

    invalid_variables = prepare_bound_tenant_twilio_message(
        tenant_id=tenant.id,
        channel="whatsapp",
        expected_sender_binding=binding,
        recipient="+5492613333333",
        template_registry_id=template.id,
        content_variables={
            "1": "contenido valido",
            "status_callback": "https://attacker.example.test/callback",
        },
    )

    assert invalid_variables.prepared is None
    assert invalid_variables.reason_code == "whatsapp_template_variables_invalid"
    with pytest.raises(TypeError, match="unexpected keyword argument 'content_sid'"):
        prepare_bound_tenant_twilio_message(
            tenant_id=tenant.id,
            channel="whatsapp",
            expected_sender_binding=binding,
            recipient="+5492613333333",
            template_registry_id=template.id,
            content_sid="HX" + ("e" * 32),
        )


def test_rejects_template_without_fresh_provider_approval(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _sender = _seed_twilio_tenant(
        app=app,
        monkeypatch=monkeypatch,
        owner_user=owner_user,
        slug="approval-required",
    )
    template = _approved_template(tenant, suffix="e-pending")
    template.status = "pending"
    db.session.flush()
    binding = build_tenant_twilio_sender_binding(
        tenant_id=tenant.id,
        channel="whatsapp",
    )

    preflight = prepare_bound_tenant_twilio_message(
        tenant_id=tenant.id,
        channel="whatsapp",
        expected_sender_binding=binding,
        recipient="+5492613333333",
        template_registry_id=template.id,
        content_variables={"1": "Marcelo"},
    )

    assert preflight.prepared is None
    assert preflight.reason_code == "whatsapp_template_not_approved"


def test_provider_call_hook_marks_exact_pre_io_boundary_without_network():
    prepared = PreparedTenantTwilioMessage(
        account_sid="AC" + ("1" * 32),
        auth_token="private-token",
        provider_sender_id=7,
        params={
            "from_": "whatsapp:+15005550001",
            "to": "whatsapp:+5492613333333",
            "content_sid": "HX" + ("a" * 32),
        },
    )
    events: list[str] = []
    fake_client = MagicMock()

    def construct_client(_account_sid, _auth_token):
        events.append("client_constructed")
        return fake_client

    def provider_create(**_params):
        events.append("provider_call_started")
        raise RuntimeError("ambiguous-provider-result")

    fake_client.messages.create.side_effect = provider_create
    with (
        patch(
            "services.tenant_twilio_messaging.require_provider_network",
            side_effect=lambda _provider: events.append("network_guard_passed"),
        ),
        patch(
            "services.tenant_twilio_messaging.Client",
            side_effect=construct_client,
        ),
        pytest.raises(RuntimeError, match="ambiguous-provider-result"),
    ):
        send_prepared_tenant_twilio_message(
            prepared,
            on_provider_call_start=lambda: events.append("provider_call_hook"),
        )

    assert events == [
        "network_guard_passed",
        "client_constructed",
        "provider_call_hook",
        "provider_call_started",
    ]


def test_invalid_provider_call_hook_fails_before_network_or_client():
    prepared = PreparedTenantTwilioMessage(
        account_sid="AC" + ("1" * 32),
        auth_token="private-token",
        provider_sender_id=7,
        params={"body": "mensaje", "from_": "+15005550001", "to": "+15005550002"},
    )

    with (
        patch("services.tenant_twilio_messaging.require_provider_network") as guard,
        patch("services.tenant_twilio_messaging.Client") as constructor,
        pytest.raises(TypeError, match="tenant_twilio_provider_call_hook_invalid"),
    ):
        send_prepared_tenant_twilio_message(
            prepared,
            on_provider_call_start=SimpleNamespace(),
        )

    guard.assert_not_called()
    constructor.assert_not_called()
