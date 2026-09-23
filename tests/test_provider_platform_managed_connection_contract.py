import copy

import pytest

from models import ProviderConnection, ProviderSender, TenantProfile, db
from services import provider_platform
from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER
from services.provider_connection_cutover_contract import (
    MANAGED_CONNECTION_CONTRACT_VERSION,
)


ACCOUNT_SID = "AC0123456789abcdef0123456789abcdef"
SENDER_SID = "XE0123456789abcdef0123456789abcdef"
SERVICE_SID = "MG0123456789abcdef0123456789abcdef"
PHONE = "+17432643718"
TOKEN_ENV = "JUNIN_TWILIO_AUTH_TOKEN"


def _seed(owner_user):
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
        configuracion={},
    )
    db.session.add(tenant)
    db.session.flush()
    marker = {
        "contract_version": MANAGED_CONNECTION_CONTRACT_VERSION,
        "enabled": True,
        "promotion_required": False,
        "provider_snapshot_sha256": "a" * 64,
        "credential_attestation_sha256": "b" * 64,
        "destination_deployment_revision": "c" * 40,
    }
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id=ACCOUNT_SID,
        credentials_ref=f"env:{TOKEN_ENV}",
        config={MANAGED_CONNECTION_MARKER: marker, "operator_note": "preserve"},
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=PHONE,
        sender_id=f"whatsapp:{PHONE}",
        sender_sid=SENDER_SID,
        messaging_service_sid=SERVICE_SID,
        status="online",
        webhook_url="https://candidate.example.test/webhook/whatsapp",
        status_callback_url="https://candidate.example.test/twilio/whatsapp/status",
    )
    db.session.add(sender)
    db.session.commit()
    return tenant, connection, sender


def _config():
    return {
        "TWILIO_TECH_PROVIDER_LIVE_ENABLED": True,
        "PUBLIC_API_BASE_URL": "https://candidate.example.test",
        "CUTOVER_DATABASE_IDENTITY_SHA256": "c" * 64,
    }


def _state(**overrides):
    value = {
        "twilio_account_sid": ACCOUNT_SID,
        "twilio_subaccount_token_ref": TOKEN_ENV,
        "sender_status": "online",
        "phone_number": PHONE,
        "sender_id": f"whatsapp:{PHONE}",
        "sender_sid": SENDER_SID,
        "messaging_service_sid": SERVICE_SID,
    }
    value.update(overrides)
    return value


def test_managed_sync_requires_postgres_serializable_lock_before_writes(
    client,
    owner_user,
):
    tenant, connection, sender = _seed(owner_user)
    original_config = copy.deepcopy(connection.config)
    with pytest.raises(
        provider_platform.ManagedProviderConnectionStateError,
        match="managed_provider_connection_postgresql_required",
    ):
        provider_platform.sync_twilio_provider_records(
            tenant,
            _state(),
            app_config=_config(),
        )
    db.session.refresh(connection)
    db.session.refresh(sender)
    assert connection.config == original_config
    assert connection.credentials_ref == f"env:{TOKEN_ENV}"
    assert connection.status == "online"
    assert sender.phone_number == PHONE
    assert sender.sender_sid == SENDER_SID


def test_managed_online_sender_drift_fails_before_any_authoritative_field_write(
    client,
    owner_user,
    monkeypatch,
):
    tenant, connection, sender = _seed(owner_user)
    original_config = copy.deepcopy(connection.config)
    monkeypatch.setattr(
        provider_platform,
        "_get_or_create_connection",
        lambda *_args, **_kwargs: connection,
    )
    with pytest.raises(
        provider_platform.ManagedProviderConnectionStateError,
        match="managed_provider_sender_phone_drift",
    ):
        provider_platform.sync_twilio_provider_records(
            tenant,
            _state(phone_number="+17432640000"),
            app_config=_config(),
        )
    db.session.refresh(connection)
    db.session.refresh(sender)
    assert connection.config == original_config
    assert connection.credentials_ref == f"env:{TOKEN_ENV}"
    assert connection.status == "online"
    assert sender.phone_number == PHONE
    assert sender.sender_id == f"whatsapp:{PHONE}"
    assert sender.sender_sid == SENDER_SID
    assert sender.messaging_service_sid == SERVICE_SID
