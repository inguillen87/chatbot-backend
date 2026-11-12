import json
from datetime import datetime, timezone

import pytest

from models import (
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    User,
    WhatsappNumero,
    db,
)
from scripts import audit_junin_whatsapp_sender as audit


OFFICIAL_PHONE = "+17432643718"
EMPLOYEE_PHONE = "+5492613168608"
PROFILE_CONTACT_PHONE = "+5492615550101"
ACCOUNT_SID = "AC1234567890ABCDEF"
SENDER_SID = "XE1234567890ABCDEF"
MESSAGING_SERVICE_SID = "MG1234567890ABCDEF"
WEBHOOK_URL = "https://api.chatboc.ar/webhook/whatsapp"
CALLBACK_URL = "https://api.chatboc.ar/twilio/whatsapp/status"
DATABASE_IDENTITY = "a" * 64
NOW = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)


class ReadOnlySnapshotSource:
    def __init__(self, snapshot):
        self.snapshot = dict(snapshot)
        self.calls = []

    def fetch_sender_snapshot(self, *, credential_reference, expected_phone):
        self.calls.append(
            {
                "credential_reference": credential_reference,
                "expected_phone": expected_phone,
            }
        )
        return dict(self.snapshot)


def _request():
    return audit.AuditRequest(
        expected_phone=OFFICIAL_PHONE,
        expected_webhook_url=WEBHOOK_URL,
        expected_status_callback_url=CALLBACK_URL,
        database_identity_sha256=DATABASE_IDENTITY,
        maximum_snapshot_age_seconds=900,
    )


def _snapshot(**overrides):
    payload = {
        "contract_version": audit.PROVIDER_SNAPSHOT_CONTRACT,
        "source_kind": "provider_api_read",
        "read_only": True,
        "messages_sent": False,
        "mutations_performed": False,
        "resource_count": 1,
        "provider": "twilio",
        "channel": "whatsapp",
        "environment": "production",
        "evidence_id": "twilio-ro-junin-20260829-001",
        "observed_at": "2026-08-29T17:55:00Z",
        "account_sid": ACCOUNT_SID,
        "credential_account_sid": ACCOUNT_SID,
        "sender_sid": SENDER_SID,
        "messaging_service_sid": MESSAGING_SERVICE_SID,
        "phone_number": OFFICIAL_PHONE,
        "status": "online",
        "webhook_url": WEBHOOK_URL,
        "status_callback_url": CALLBACK_URL,
    }
    payload.update(overrides)
    return payload


def _seed_ready_scope(owner_user):
    owner_user.telefono = PROFILE_CONTACT_PHONE
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junin",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
        whatsapp_sender_id=f"whatsapp:{OFFICIAL_PHONE}",
        dispatch_phone=PROFILE_CONTACT_PHONE,
        configuracion={"employee_contact_phone": EMPLOYEE_PHONE},
    )
    db.session.add(tenant)
    db.session.flush()
    routing = WhatsappNumero(
        numero_whatsapp=OFFICIAL_PHONE,
        user_id=owner_user.id,
        is_active=True,
    )
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id=ACCOUNT_SID,
        credentials_ref="env:TWILIO_JUNIN_READ_ONLY",
    )
    db.session.add_all([routing, connection])
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=OFFICIAL_PHONE,
        sender_id=f"whatsapp:{OFFICIAL_PHONE}",
        sender_sid=SENDER_SID,
        messaging_service_sid=MESSAGING_SERVICE_SID,
        status="active",
        webhook_url=WEBHOOK_URL,
        status_callback_url=CALLBACK_URL,
    )
    db.session.add(sender)
    db.session.commit()
    return tenant, connection, sender, routing


def test_certifies_exact_junin_binding_without_writes_or_sensitive_output(
    client,
    owner_user,
):
    tenant, connection, sender, routing = _seed_ready_scope(owner_user)
    source = ReadOnlySnapshotSource(_snapshot())
    before = {
        "tenant": TenantProfile.query.count(),
        "connection": ProviderConnection.query.count(),
        "sender": ProviderSender.query.count(),
        "routing": WhatsappNumero.query.count(),
    }

    result = audit.audit_junin_sender(
        db.session,
        _request(),
        source,
        now=NOW,
    )

    assert result["status"] == "certified"
    assert result["official_phone"]["last4"] == "3718"
    assert result["database"]["ready_sender_count"] == 1
    assert result["checks"] == {
        "tenant_exact": True,
        "profile_sender_exact": True,
        "employee_and_profile_contact_phones_excluded": True,
        "inbound_route_owned": True,
        "single_ready_sender": True,
        "provider_and_credential_owner_exact": True,
        "webhook_and_callback_exact": True,
    }
    assert result["audit_controls"] == {
        "database_read_only": True,
        "provider_read_only": True,
        "messages_sent": False,
        "database_mutations": False,
        "provider_mutations": False,
    }
    assert source.calls == [
        {
            "credential_reference": connection.credentials_ref,
            "expected_phone": OFFICIAL_PHONE,
        }
    ]
    assert {
        "tenant": TenantProfile.query.count(),
        "connection": ProviderConnection.query.count(),
        "sender": ProviderSender.query.count(),
        "routing": WhatsappNumero.query.count(),
    } == before
    db.session.refresh(tenant)
    db.session.refresh(sender)
    db.session.refresh(routing)
    assert tenant.whatsapp_sender_id == f"whatsapp:{OFFICIAL_PHONE}"
    assert sender.status == "active"
    assert routing.is_active is True

    rendered = json.dumps(result, sort_keys=True)
    for sensitive in (
        OFFICIAL_PHONE,
        EMPLOYEE_PHONE,
        PROFILE_CONTACT_PHONE,
        ACCOUNT_SID,
        SENDER_SID,
        MESSAGING_SERVICE_SID,
        WEBHOOK_URL,
        CALLBACK_URL,
        connection.credentials_ref,
    ):
        assert sensitive not in rendered


def test_employee_and_profile_contact_numbers_are_not_sender_candidates(
    client,
    owner_user,
):
    _seed_ready_scope(owner_user)
    employee = User(
        name="Empleado luminarias",
        email="employee-luminarias@example.test",
        telefono=EMPLOYEE_PHONE,
        rol="empleado",
    )
    employee.set_password("test-only")
    db.session.add(employee)
    db.session.commit()

    result = audit.audit_junin_sender(
        db.session,
        _request(),
        ReadOnlySnapshotSource(_snapshot()),
        now=NOW,
    )

    assert result["status"] == "certified"
    assert result["checks"]["employee_and_profile_contact_phones_excluded"] is True
    rendered = json.dumps(result, sort_keys=True)
    assert EMPLOYEE_PHONE not in rendered
    assert PROFILE_CONTACT_PHONE not in rendered


@pytest.mark.parametrize(
    ("snapshot_override", "reason_code"),
    [
        ({"credential_account_sid": "AC9999999999ZZZZZZ"}, "provider_credential_ownership_mismatch"),
        ({"webhook_url": "https://wrong.example.test/webhook/whatsapp"}, "provider_webhook_url_mismatch"),
        ({"status_callback_url": "https://wrong.example.test/status"}, "provider_callback_url_mismatch"),
        ({"status": "pending"}, "provider_sender_not_ready"),
        ({"resource_count": 2}, "provider_sender_resource_count_mismatch"),
        ({"read_only": False}, "provider_snapshot_not_read_only"),
        ({"observed_at": "2026-08-29T17:00:00Z"}, "provider_snapshot_stale"),
    ],
)
def test_provider_snapshot_fail_closed(
    client,
    owner_user,
    snapshot_override,
    reason_code,
):
    _seed_ready_scope(owner_user)

    with pytest.raises(audit.SenderAuditError, match=reason_code):
        audit.audit_junin_sender(
            db.session,
            _request(),
            ReadOnlySnapshotSource(_snapshot(**snapshot_override)),
            now=NOW,
        )


def test_multiple_ready_senders_block_before_provider_read(client, owner_user):
    tenant, connection, _, _ = _seed_ready_scope(owner_user)
    db.session.add(
        ProviderSender(
            tenant_id=tenant.id,
            provider_connection_id=connection.id,
            channel="whatsapp",
            phone_number="+15550009999",
            sender_id="whatsapp:+15550009999",
            sender_sid="XE9999999999ZZZZZZ",
            messaging_service_sid=MESSAGING_SERVICE_SID,
            status="active",
            webhook_url=WEBHOOK_URL,
            status_callback_url=CALLBACK_URL,
        )
    )
    db.session.commit()
    source = ReadOnlySnapshotSource(_snapshot())

    with pytest.raises(
        audit.SenderAuditError,
        match="provider_sender_ready_exactly_one_required",
    ):
        audit.audit_junin_sender(db.session, _request(), source, now=NOW)

    assert source.calls == []


def test_cross_tenant_provider_claim_blocks_before_provider_read(client, owner_user):
    _tenant, _connection, _sender, _routing = _seed_ready_scope(owner_user)
    other_owner = User(
        name="Otro owner",
        email="other-owner-sender-audit@example.test",
        rol="admin",
    )
    other_owner.set_password("test-only")
    db.session.add(other_owner)
    db.session.flush()
    other_tenant = TenantProfile(
        slug="other-municipality",
        nombre="Otro municipio",
        tipo="municipio",
        municipio_id=other_owner.id,
        is_active=True,
    )
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(
        ProviderSender(
            tenant_id=other_tenant.id,
            channel="whatsapp",
            phone_number=OFFICIAL_PHONE,
            sender_id=f"whatsapp:{OFFICIAL_PHONE}",
            status="draft",
        )
    )
    db.session.commit()
    source = ReadOnlySnapshotSource(_snapshot())

    with pytest.raises(
        audit.SenderAuditError,
        match="official_phone_claimed_by_other_provider_sender",
    ):
        audit.audit_junin_sender(db.session, _request(), source, now=NOW)

    assert source.calls == []


def test_request_reads_sensitive_values_only_from_named_environment_variables():
    environ = {
        "SAFE_PHONE": OFFICIAL_PHONE,
        "SAFE_WEBHOOK": WEBHOOK_URL,
        "SAFE_CALLBACK": CALLBACK_URL,
    }

    request = audit.build_request_from_environment(
        database_identity_sha256=DATABASE_IDENTITY,
        phone_environment_variable="SAFE_PHONE",
        webhook_environment_variable="SAFE_WEBHOOK",
        callback_environment_variable="SAFE_CALLBACK",
        environ=environ,
    )

    assert request == _request()
    help_text = audit._parser().format_help()
    assert "--apply" not in help_text
    assert "--tenant" not in help_text
    assert "--official-phone" not in help_text


def test_expected_phone_is_pinned_to_junin_official_last4():
    with pytest.raises(
        audit.SenderAuditError,
        match="expected_phone_not_junin_official_last4",
    ):
        audit.build_request_from_environment(
            database_identity_sha256=DATABASE_IDENTITY,
            phone_environment_variable="SAFE_PHONE",
            webhook_environment_variable="SAFE_WEBHOOK",
            callback_environment_variable="SAFE_CALLBACK",
            environ={
                "SAFE_PHONE": "+15550009999",
                "SAFE_WEBHOOK": WEBHOOK_URL,
                "SAFE_CALLBACK": CALLBACK_URL,
            },
        )


def test_environment_provider_source_only_decodes_read_snapshot():
    snapshot = _snapshot()
    source = audit.EnvironmentProviderSnapshotSource(
        environment_variable="SAFE_PROVIDER_SNAPSHOT",
        environ={"SAFE_PROVIDER_SNAPSHOT": json.dumps(snapshot)},
    )

    assert source.fetch_sender_snapshot(
        credential_reference="env:not-exposed",
        expected_phone=OFFICIAL_PHONE,
    ) == snapshot
    assert not hasattr(source, "send")
    assert not hasattr(source, "update")
    assert not hasattr(source, "delete")


def test_failure_payload_is_stable_and_contains_no_exception_details():
    result = audit._failure_payload("provider_sender_not_ready")

    assert result == {
        "contract_version": audit.CONTRACT_VERSION,
        "status": "blocked",
        "reason_code": "provider_sender_not_ready",
        "audit_controls": {
            "messages_sent": False,
            "database_mutations": False,
            "provider_mutations": False,
        },
    }
