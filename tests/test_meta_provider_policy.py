"""Meta opt-in must not rotate Twilio identity or widen its queue scope."""
from copy import deepcopy

import pytest

from config import validate_runtime_security
from services.channel_session_identity import (
    derive_channel_session_identity_hmac, resolve_channel_session_identity_secret,
)
from services.whatsapp_inbound_durability import (
    WhatsAppInboundDurabilityConfigurationError,
    resolve_whatsapp_inbound_durability_policy,
    resolve_whatsapp_inbound_queue_tenants,
)
from services.whatsapp_provider_policy import (
    IDENTITY_SECRET, IDENTITY_VERSION, ISOLATION_ENABLED, QUEUE_TENANTS,
    STREAM_SECRET, WhatsAppProviderPolicyError, whatsapp_provider_config,
)
from tests.test_whatsapp_durable_runtime_config import _production_config


def _isolated_config(**changes):
    config = _production_config(
        CHANNEL_SESSION_IDENTITY_MODE="shadow",
        META_WHATSAPP_PROVIDER_ISOLATION_ENABLED=True,
        META_WHATSAPP_QUEUE_TENANT_IDS="22",
        META_WHATSAPP_INBOUND_HASH_SECRET="synthetic-meta-stream-" + "s" * 32,
        META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="synthetic-meta-identity-" + "i" * 32,
        META_CHANNEL_SESSION_IDENTITY_VERSION="meta-v1",
    )
    config.update(changes)
    return config


def test_isolated_meta_preserves_twilio_fallback_digest_and_global_config():
    config = _isolated_config()
    before = deepcopy(config)
    old_secret = resolve_channel_session_identity_secret(config)
    arguments = dict(tenant_id=22, provider="twilio", identity_version="v1",
                     provider_identity="+15550001111")
    old_digest = derive_channel_session_identity_hmac(secret=old_secret, **arguments)

    meta = whatsapp_provider_config(config, "meta")
    assert meta is not config and config == before
    assert meta["CHANNEL_SESSION_IDENTITY_MODE"] == "enforce"
    assert meta["CHANNEL_SESSION_IDENTITY_VERSION_V1"] == "meta-v1"
    assert resolve_channel_session_identity_secret(meta) == config[IDENTITY_SECRET].encode()
    assert meta["WHATSAPP_INBOUND_HASH_SECRET"] == config[STREAM_SECRET]
    assert resolve_whatsapp_inbound_queue_tenants(config, provider="meta") == frozenset({22})
    assert resolve_whatsapp_inbound_queue_tenants(config) == frozenset()
    assert resolve_whatsapp_inbound_durability_policy(
        config, tenant_id=22, provider="meta").queue_enabled
    assert not resolve_whatsapp_inbound_durability_policy(
        config, tenant_id=23, provider="meta").queue_enabled
    assert not resolve_whatsapp_inbound_durability_policy(config, tenant_id=22).queue_enabled

    twilio = whatsapp_provider_config(config, "twilio")
    assert twilio is config and config == before
    assert resolve_channel_session_identity_secret(twilio) == old_secret == config["SECRET_KEY"].encode()
    assert derive_channel_session_identity_hmac(
        secret=resolve_channel_session_identity_secret(twilio), **arguments) == old_digest
    meta["CHANNEL_SESSION_IDENTITY_MODE"] = "legacy"
    assert config == before


def test_disabled_isolation_preserves_previous_meta_contract():
    config = _production_config(
        WHATSAPP_INBOUND_DURABILITY_MODE="queue",
        WHATSAPP_INBOUND_HASH_SECRET="global-stream-" + "s" * 32,
        CHANNEL_SESSION_IDENTITY_MODE="enforce",
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="global-identity-" + "i" * 32,
    )
    assert whatsapp_provider_config(config, "meta") is config
    assert resolve_whatsapp_inbound_queue_tenants(config, provider="meta") == frozenset({7})
    config[ISOLATION_ENABLED] = False
    assert whatsapp_provider_config(config, "meta") is config
    assert validate_runtime_security(config) == []


@pytest.mark.parametrize("flag", (None, 1, "true", "false", {}))
def test_nonboolean_isolation_flag_fails_closed_without_affecting_twilio_view(flag):
    config = _isolated_config(**{ISOLATION_ENABLED: flag})
    with pytest.raises(WhatsAppProviderPolicyError, match="meta_provider_isolation_flag_invalid"):
        whatsapp_provider_config(config, "meta")
    assert whatsapp_provider_config(config, "twilio") is config
    errors = validate_runtime_security(config)
    assert any("meta_provider_isolation_flag_invalid" in error for error in errors)


@pytest.mark.parametrize("key,code", (
    (STREAM_SECRET, "meta_provider_stream_secret_missing"),
    (IDENTITY_SECRET, "meta_provider_identity_secret_missing"),
))
@pytest.mark.parametrize("value", (None, "", "x" * 31, b"x" * 32))
def test_missing_meta_secret_never_falls_back_to_valid_global_secrets(key, code, value):
    config = _isolated_config(
        WHATSAPP_INBOUND_HASH_SECRET="global-stream-" + "s" * 32,
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="global-identity-" + "i" * 32,
        **{key: value},
    )
    before = deepcopy(config)
    with pytest.raises(WhatsAppProviderPolicyError, match=code):
        resolve_whatsapp_inbound_queue_tenants(config, provider="meta")
    errors = validate_runtime_security(config)
    assert any(code in error for error in errors)
    assert all(config["WHATSAPP_INBOUND_HASH_SECRET"] not in error
               and config["CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"] not in error for error in errors)
    assert config == before


@pytest.mark.parametrize("version", (None, "", "v1", "meta-", "META-v1", "meta-" + "x" * 28))
def test_meta_identity_version_must_be_explicit_and_distinct(version):
    config = _isolated_config(**{IDENTITY_VERSION: version})
    with pytest.raises(WhatsAppProviderPolicyError, match="meta_provider_identity_version_invalid"):
        whatsapp_provider_config(config, "meta")
    assert any("meta_provider_identity_version_invalid" in error
               for error in validate_runtime_security(config))


@pytest.mark.parametrize("tenants", (None, "", " ", 22, "22,0", "22,foreign"))
def test_meta_requires_a_valid_explicit_tenant_allowlist(tenants):
    config = _isolated_config(**{QUEUE_TENANTS: tenants})
    with pytest.raises((WhatsAppProviderPolicyError, WhatsAppInboundDurabilityConfigurationError)):
        resolve_whatsapp_inbound_queue_tenants(config, provider="meta")
    assert any("META_WHATSAPP_PROVIDER_ISOLATION_ENABLED" in error
               for error in validate_runtime_security(config))
    assert resolve_whatsapp_inbound_queue_tenants(config) == frozenset()


@pytest.mark.parametrize("mode", ("legacy", "shadow"))
def test_production_meta_config_accepts_unchanged_legacy_or_shadow_twilio(mode):
    config = _isolated_config(CHANNEL_SESSION_IDENTITY_MODE=mode)
    assert validate_runtime_security(config) == []
    assert not config["WHATSAPP_INBOUND_HASH_SECRET"]
    assert not config["CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"]
    assert config["CHANNEL_SESSION_IDENTITY_MODE"] == mode


@pytest.mark.parametrize("flag", ("META_WHATSAPP_INBOUND_WORKER_ENABLED", "META_WHATSAPP_OUTBOUND_ENABLED"))
def test_durable_worker_accepts_explicit_meta_only_activation(flag):
    config = _isolated_config(CHATBOC_PROCESS_ROLE="whatsapp-durable-worker", **{flag: True})
    assert validate_runtime_security(config) == []
    config[flag] = False
    assert any("whatsapp-durable-worker" in error for error in validate_runtime_security(config))


@pytest.mark.parametrize("key,value", (
    ("WHATSAPP_INBOUND_LEASE_SECONDS", 5),
    ("WHATSAPP_INBOUND_WORKER_BATCH_SIZE", 0),
    ("WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES", 65537),
    ("WHATSAPP_INBOUND_WORKER_POLL_SECONDS", 61),
))
def test_meta_only_queue_still_validates_worker_resource_bounds(key, value):
    assert any(key in error for error in validate_runtime_security(_isolated_config(**{key: value})))


def test_meta_configuration_does_not_relax_global_twilio_queue_requirements():
    config = _isolated_config(WHATSAPP_INBOUND_DURABILITY_MODE="queue")
    errors = validate_runtime_security(config)
    assert any("WHATSAPP_INBOUND_DURABILITY_MODE=queue requiere" in error for error in errors)
    assert any("WHATSAPP_INBOUND_HASH_SECRET" in error for error in errors)
    with pytest.raises(WhatsAppInboundDurabilityConfigurationError):
        resolve_whatsapp_inbound_queue_tenants(config)
    assert resolve_whatsapp_inbound_queue_tenants(config, provider="meta") == frozenset({22})
