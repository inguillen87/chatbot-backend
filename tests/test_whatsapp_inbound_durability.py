import pytest

from services.whatsapp_inbound_durability import (
    WhatsAppInboundDurabilityConfigurationError,
    resolve_whatsapp_inbound_durability_policy,
    resolve_whatsapp_inbound_queue_tenants,
)


def _config(**overrides):
    config = {
        "WHATSAPP_INBOUND_DURABILITY_MODE": "queue",
        "WHATSAPP_INBOUND_QUEUE_TENANT_IDS": "7,11",
        "WHATSAPP_INBOUND_HASH_SECRET": "q" * 32,
        "CHANNEL_SESSION_IDENTITY_MODE": "enforce",
        "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1": "i" * 32,
    }
    config.update(overrides)
    return config


def test_queue_policy_enables_only_explicit_tenant_canaries():
    included = resolve_whatsapp_inbound_durability_policy(
        _config(),
        tenant_id=7,
    )
    excluded = resolve_whatsapp_inbound_durability_policy(
        _config(),
        tenant_id=8,
    )

    assert included.queue_enabled is True
    assert excluded.queue_enabled is False
    assert resolve_whatsapp_inbound_queue_tenants(_config()) == frozenset({7, 11})


def test_legacy_mode_keeps_every_tenant_synchronous_even_with_stale_allowlist():
    policy = resolve_whatsapp_inbound_durability_policy(
        _config(WHATSAPP_INBOUND_DURABILITY_MODE="legacy"),
        tenant_id=7,
    )

    assert policy.mode == "legacy"
    assert policy.queue_enabled is False
    assert resolve_whatsapp_inbound_queue_tenants(
        _config(WHATSAPP_INBOUND_DURABILITY_MODE="legacy")
    ) == frozenset()


def test_legacy_mode_keeps_authoritative_tenantless_routes_synchronous():
    policy = resolve_whatsapp_inbound_durability_policy(
        _config(
            WHATSAPP_INBOUND_DURABILITY_MODE="legacy",
            WHATSAPP_INBOUND_QUEUE_TENANT_IDS="",
            WHATSAPP_INBOUND_HASH_SECRET="",
            CHANNEL_SESSION_IDENTITY_MODE="legacy",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="",
        ),
        tenant_id=None,
    )

    assert policy.mode == "legacy"
    assert policy.tenant_id is None
    assert policy.queue_enabled is False


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        (
            {"WHATSAPP_INBOUND_QUEUE_TENANT_IDS": ""},
            "whatsapp_inbound_queue_tenant_allowlist_required",
        ),
        (
            {"WHATSAPP_INBOUND_QUEUE_TENANT_IDS": "7,not-a-tenant"},
            "whatsapp_inbound_queue_tenant_invalid",
        ),
        (
            {"WHATSAPP_INBOUND_QUEUE_TENANT_IDS": "7,0"},
            "whatsapp_inbound_queue_tenant_invalid",
        ),
        (
            {"WHATSAPP_INBOUND_HASH_SECRET": "short"},
            "whatsapp_inbound_hash_secret_invalid",
        ),
        (
            {"CHANNEL_SESSION_IDENTITY_MODE": "shadow"},
            "whatsapp_inbound_session_identity_not_enforced",
        ),
        (
            {"CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1": "short"},
            "whatsapp_inbound_session_identity_secret_invalid",
        ),
    ],
)
def test_queue_configuration_fails_closed(overrides, error_code):
    with pytest.raises(WhatsAppInboundDurabilityConfigurationError, match=error_code):
        resolve_whatsapp_inbound_queue_tenants(_config(**overrides))


def test_invalid_or_missing_trusted_tenant_never_falls_back_to_legacy():
    for invalid_tenant in (None, True, 0, -1):
        with pytest.raises(
            WhatsAppInboundDurabilityConfigurationError,
            match="whatsapp_inbound_tenant_invalid",
        ):
            resolve_whatsapp_inbound_durability_policy(
                _config(),
                tenant_id=invalid_tenant,
            )
