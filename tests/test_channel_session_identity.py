from __future__ import annotations

import uuid

import pytest

from app import create_app
from config import TestConfig
from extensions import db
from models import (
    ChannelSessionIdentityBinding,
    ChatSessionContext,
    TenantProfile,
    User,
)
from services.channel_session_identity import (
    ChannelSessionIdentityConflict,
    derive_channel_session_identity_hmac,
    resolve_channel_session_identity,
    verify_channel_session_identity_binding,
)
from services.voice_handler import _resolve_voice_session_context


IDENTITY_SECRET = "canonical-session-identity-test-secret-0000000001"
PHONE = "+5492613168608"


class IdentityConfig(TestConfig):
    CHANNEL_SESSION_IDENTITY_MODE = "enforce"
    CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1 = IDENTITY_SECRET
    CHANNEL_SESSION_IDENTITY_VERSION_V1 = "v1"


@pytest.fixture()
def identity_app():
    app = create_app(IdentityConfig)
    with app.app_context():
        db.create_all()
        try:
            yield app
        finally:
            db.session.remove()
            db.drop_all()


def _owner_and_tenant(label: str) -> tuple[User, TenantProfile]:
    owner = User(
        name=f"Owner {label}",
        email=f"owner-{label}@example.test",
        rol="empresa",
        tipo_chat="pyme",
    )
    owner.set_password("test-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"tenant-{label}",
        nombre=f"Tenant {label}",
        tipo="pyme",
        pyme_id=owner.id,
        configuracion={},
    )
    db.session.add(tenant)
    db.session.commit()
    return owner, tenant


def _resolve(
    app,
    *,
    tenant: TenantProfile,
    owner: User,
    channel: str = "whatsapp",
    explicit_legacy_chat_session_id: str | None = None,
    create_if_missing: bool = True,
):
    return resolve_channel_session_identity(
        config=app.config,
        tenant_id=tenant.id,
        channel=channel,
        provider="twilio",
        provider_identity=PHONE,
        owner_user_id=owner.id,
        explicit_legacy_chat_session_id=explicit_legacy_chat_session_id,
        create_if_missing=create_if_missing,
    )


def test_same_citizen_is_isolated_per_tenant_and_binding_contains_no_pii(identity_app):
    first_owner, first_tenant = _owner_and_tenant("first")
    second_owner, second_tenant = _owner_and_tenant("second")

    first = _resolve(identity_app, tenant=first_tenant, owner=first_owner)
    replay = _resolve(identity_app, tenant=first_tenant, owner=first_owner)
    second = _resolve(identity_app, tenant=second_tenant, owner=second_owner)

    assert first is not None and replay is not None and second is not None
    assert replay.binding_id == first.binding_id
    assert replay.chat_session_id == first.chat_session_id
    assert second.binding_id != first.binding_id
    assert second.chat_session_id != first.chat_session_id
    assert second.identity_hmac != first.identity_hmac
    assert PHONE not in first.chat_session_id
    binding = db.session.get(ChannelSessionIdentityBinding, first.binding_id)
    serialized = repr(
        {
            "channel": binding.channel,
            "provider": binding.provider,
            "identity_version": binding.identity_version,
            "identity_hmac": binding.identity_hmac,
            "chat_session_id": binding.chat_session_id,
        }
    )
    assert PHONE not in serialized


def test_hmac_is_versioned_and_never_echoes_provider_identity():
    first = derive_channel_session_identity_hmac(
        secret=IDENTITY_SECRET,
        tenant_id=7,
        provider="twilio",
        identity_version="v1",
        provider_identity=f"whatsapp:{PHONE}",
    )
    second = derive_channel_session_identity_hmac(
        secret=IDENTITY_SECRET,
        tenant_id=7,
        provider="twilio",
        identity_version="v2",
        provider_identity=PHONE,
    )

    assert len(first) == 64
    assert PHONE not in first
    assert first != second


def test_legacy_context_is_adopted_only_when_membership_is_unique(identity_app):
    owner, tenant = _owner_and_tenant("adopt")
    legacy_id = "legacy-safe-session"
    legacy = ChatSessionContext(
        chat_session_id=legacy_id,
        tenant_id=None,
        user_id=owner.id,
        anon_id=PHONE,
        context_data={"history_marker": "preserve-me"},
    )
    db.session.add(legacy)
    db.session.commit()

    resolution = _resolve(identity_app, tenant=tenant, owner=owner)

    assert resolution is not None
    assert resolution.outcome == "adopted"
    assert resolution.chat_session_id == legacy_id
    assert resolution.continuity_preserved is True
    db.session.expire_all()
    adopted = db.session.get(ChatSessionContext, legacy_id)
    assert adopted.tenant_id == tenant.id
    assert adopted.context_data["history_marker"] == "preserve-me"


def test_ambiguous_legacy_contexts_are_quarantined_and_preserved(identity_app):
    owner, tenant = _owner_and_tenant("ambiguous")
    legacy_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for index, legacy_id in enumerate(legacy_ids):
        db.session.add(
            ChatSessionContext(
                chat_session_id=legacy_id,
                tenant_id=tenant.id,
                user_id=owner.id,
                anon_id=PHONE,
                context_data={"message": f"legacy-{index}"},
            )
        )
    db.session.commit()

    resolution = _resolve(identity_app, tenant=tenant, owner=owner)

    assert resolution is not None
    assert resolution.outcome == "isolated"
    assert resolution.continuity_preserved is False
    assert resolution.chat_session_id not in legacy_ids
    binding = db.session.get(ChannelSessionIdentityBinding, resolution.binding_id)
    assert binding.continuity_status == "isolated"
    assert binding.last_conflict_code == "legacy_membership_ambiguous"
    assert binding.quarantined_at is not None
    assert binding.previous_session_digest is not None
    assert all(db.session.get(ChatSessionContext, item) is not None for item in legacy_ids)
    assert {
        db.session.get(ChatSessionContext, item).context_data["message"]
        for item in legacy_ids
    } == {"legacy-0", "legacy-1"}


def test_durable_binding_verification_fails_closed_after_context_scope_change(identity_app):
    owner, tenant = _owner_and_tenant("verify")
    other_owner, other_tenant = _owner_and_tenant("verify-other")
    resolution = _resolve(identity_app, tenant=tenant, owner=owner)
    assert resolution is not None

    verified = verify_channel_session_identity_binding(
        tenant_id=tenant.id,
        binding_id=resolution.binding_id,
        channel="whatsapp",
        provider="twilio",
        identity_version=resolution.identity_version,
        identity_hmac=resolution.identity_hmac,
        chat_session_id=resolution.chat_session_id,
    )
    assert verified.outcome == "verified"

    context = db.session.get(ChatSessionContext, resolution.chat_session_id)
    context.tenant_id = other_tenant.id
    context.user_id = other_owner.id
    db.session.commit()

    with pytest.raises(
        ChannelSessionIdentityConflict,
        match="session_identity_replay_scope_conflict",
    ):
        verify_channel_session_identity_binding(
            tenant_id=tenant.id,
            binding_id=resolution.binding_id,
            channel="whatsapp",
            provider="twilio",
            identity_version=resolution.identity_version,
            identity_hmac=resolution.identity_hmac,
            chat_session_id=resolution.chat_session_id,
        )


def test_voice_uses_distinct_canonical_context_and_verified_whatsapp_source(identity_app):
    owner, tenant = _owner_and_tenant("voice")
    whatsapp = _resolve(identity_app, tenant=tenant, owner=owner)
    assert whatsapp is not None
    whatsapp_context = db.session.get(ChatSessionContext, whatsapp.chat_session_id)
    whatsapp_context.context_data = {"verified_history": "from-whatsapp"}
    db.session.commit()

    voice_context, source_context = _resolve_voice_session_context(
        owner_user=owner,
        user_phone_clean=PHONE,
        bot_phone_clean="+5492613000000",
        call_sid="CAcanonicalvoice001",
        create_if_missing=True,
    )

    assert voice_context is not None
    assert source_context is not None
    assert source_context.chat_session_id == whatsapp.chat_session_id
    assert voice_context.chat_session_id != whatsapp.chat_session_id
    assert PHONE not in voice_context.chat_session_id
    assert voice_context.tenant_id == tenant.id
    assert voice_context.context_data["verified_history"] == "from-whatsapp"
    assert voice_context.context_data["source_chat_session_id"] == whatsapp.chat_session_id
