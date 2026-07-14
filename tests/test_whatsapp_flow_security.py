from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import db
from models import (
    MessageTemplateRegistry,
    ProviderSender,
    TenantProfile,
    User,
    WhatsAppFlowInteraction,
)
from services.whatsapp_flow_security import (
    WhatsAppFlowTokenError,
    consume_whatsapp_flow_interaction,
    issue_whatsapp_flow_token,
    verify_whatsapp_flow_token,
    whatsapp_flow_token_key_ready,
)


SECRET = "test-flow-security-key-v1-000000000000000000000000000000"
FLOW_ID = "catalog_order_builder"
META_FLOW_ID = "1232445823264765"
RECIPIENT = "+5491112345678"


@pytest.fixture(autouse=True)
def _application_context(app):
    with app.app_context():
        yield


def _seed_invocation():
    suffix = uuid4().hex
    owner = User(
        email=f"flow-security-{suffix}@test.com",
        name="Flow Security",
        rol="tenant_admin",
        tipo_chat="pyme",
    )
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"flow-security-{suffix}",
        nombre="Flow Security",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        channel="whatsapp",
        phone_number="+5491100000000",
        status="active",
    )
    db.session.add(sender)
    db.session.flush()
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="flow_security_native_v1",
        language="es",
        status="approved",
        content_sid="HXflowsecurity",
        external_template_id=META_FLOW_ID,
        metadata_json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
            "content_family": "meta_native_flow",
            "meta_flow_status": "published",
        },
    )
    db.session.add(registry)
    db.session.flush()
    issued = issue_whatsapp_flow_token(
        secret=SECRET,
        tenant_id=tenant.id,
        recipient=RECIPIENT,
        flow_id=FLOW_ID,
        meta_flow_id=META_FLOW_ID,
        provider_sender_id=sender.id,
        ttl_seconds=3600,
    )
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=FLOW_ID,
        meta_flow_id=META_FLOW_ID,
        content_sid=registry.content_sid,
        recipient_hash=issued.recipient_hash,
        recipient_hint=issued.recipient_hint,
        token_digest=issued.token_digest,
        idempotency_key=f"security-test-{suffix}",
        status="sent",
        data_contract=["catalog_items", "cart_id"],
        expires_at=issued.expires_at,
    )
    db.session.add(interaction)
    db.session.commit()
    return tenant, sender, interaction, issued


def test_flow_token_key_and_issuance_are_strict_and_unique(app):
    assert whatsapp_flow_token_key_ready(SECRET) is True
    assert whatsapp_flow_token_key_ready("short") is False
    first = issue_whatsapp_flow_token(
        secret=SECRET,
        tenant_id=7,
        recipient=RECIPIENT,
        flow_id=FLOW_ID,
        meta_flow_id=META_FLOW_ID,
        provider_sender_id=3,
        ttl_seconds=3600,
    )
    second = issue_whatsapp_flow_token(
        secret=SECRET,
        tenant_id=7,
        recipient=RECIPIENT,
        flow_id=FLOW_ID,
        meta_flow_id=META_FLOW_ID,
        provider_sender_id=3,
        ttl_seconds=3600,
    )
    assert first.token != second.token
    assert first.token_digest != second.token_digest
    assert first.recipient_hash == second.recipient_hash
    assert RECIPIENT not in first.token

    with pytest.raises(WhatsAppFlowTokenError, match="flow_token_key_not_configured"):
        issue_whatsapp_flow_token(
            secret="short",
            tenant_id=7,
            recipient=RECIPIENT,
            flow_id=FLOW_ID,
            meta_flow_id=META_FLOW_ID,
            provider_sender_id=3,
        )


def test_verification_binds_tenant_recipient_sender_and_consumes_atomically(app):
    tenant, sender, interaction, issued = _seed_invocation()
    verified = verify_whatsapp_flow_token(
        issued.token,
        secret=SECRET,
        tenant_id=tenant.id,
        recipient=RECIPIENT,
        provider_sender_id=sender.id,
        allowed_flow_ids={FLOW_ID, META_FLOW_ID},
        ttl_seconds=3600,
    )
    assert verified["interaction_id"] == interaction.id
    assert verified["data_contract"] == ["catalog_items", "cart_id"]
    assert verified["already_consumed"] is False

    with pytest.raises(WhatsAppFlowTokenError):
        verify_whatsapp_flow_token(
            f"{issued.token}tampered",
            secret=SECRET,
            tenant_id=tenant.id,
            recipient=RECIPIENT,
            provider_sender_id=sender.id,
            allowed_flow_ids={FLOW_ID},
            ttl_seconds=3600,
        )
    with pytest.raises(WhatsAppFlowTokenError, match="flow_token_scope_mismatch"):
        verify_whatsapp_flow_token(
            issued.token,
            secret=SECRET,
            tenant_id=tenant.id,
            recipient="+5491199999999",
            provider_sender_id=sender.id,
            allowed_flow_ids={FLOW_ID},
            ttl_seconds=3600,
        )
    with pytest.raises(WhatsAppFlowTokenError, match="flow_token_sender_mismatch"):
        verify_whatsapp_flow_token(
            issued.token,
            secret=SECRET,
            tenant_id=tenant.id,
            recipient=RECIPIENT,
            provider_sender_id=sender.id + 1,
            allowed_flow_ids={FLOW_ID},
            ttl_seconds=3600,
        )
    with pytest.raises(WhatsAppFlowTokenError, match="invalid_flow_token"):
        verify_whatsapp_flow_token(
            issued.token,
            secret=SECRET,
            tenant_id=tenant.id + 1,
            recipient=RECIPIENT,
            provider_sender_id=sender.id,
            allowed_flow_ids={FLOW_ID},
            ttl_seconds=3600,
        )

    assert consume_whatsapp_flow_interaction(
        interaction_id=interaction.id,
        tenant_id=tenant.id,
        inbound_message_sid="SM-CONSUME-1",
    ) is True
    assert consume_whatsapp_flow_interaction(
        interaction_id=interaction.id,
        tenant_id=tenant.id,
        inbound_message_sid="SM-CONSUME-2",
    ) is False
    consumed = db.session.get(WhatsAppFlowInteraction, interaction.id)
    assert consumed.status == "consumed"
    assert consumed.inbound_message_sid == "SM-CONSUME-1"


def test_expired_durable_invocation_is_rejected(app):
    tenant, sender, interaction, issued = _seed_invocation()
    interaction.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.add(interaction)
    db.session.commit()

    with pytest.raises(WhatsAppFlowTokenError, match="expired_flow_invocation"):
        verify_whatsapp_flow_token(
            issued.token,
            secret=SECRET,
            tenant_id=tenant.id,
            recipient=RECIPIENT,
            provider_sender_id=sender.id,
            allowed_flow_ids={FLOW_ID},
            ttl_seconds=3600,
        )
