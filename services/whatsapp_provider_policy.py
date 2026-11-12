"""Explicit Meta policy without mutating legacy provider configuration.

The previous Meta rollout contract remains available while isolation is off.
Once enabled, Meta never falls back to global identity or stream secrets.
"""
from collections.abc import Mapping
import re


ISOLATION_ENABLED = "META_WHATSAPP_PROVIDER_ISOLATION_ENABLED"
QUEUE_TENANTS = "META_WHATSAPP_QUEUE_TENANT_IDS"
STREAM_SECRET = "META_WHATSAPP_INBOUND_HASH_SECRET"
IDENTITY_SECRET = "META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"
IDENTITY_VERSION = "META_CHANNEL_SESSION_IDENTITY_VERSION"


class WhatsAppProviderPolicyError(ValueError):
    """Fixed configuration codes, never secret material."""


def meta_provider_isolation_enabled(config: Mapping) -> bool:
    value = config.get(ISOLATION_ENABLED, False)
    if type(value) is not bool:
        raise WhatsAppProviderPolicyError("meta_provider_isolation_flag_invalid")
    return value


def whatsapp_provider_config(config: Mapping, provider: str) -> Mapping:
    """Return an independent, validated view only for isolated Meta callers."""
    if provider != "meta" or not meta_provider_isolation_enabled(config):
        return config
    stream_secret, identity_secret = config.get(STREAM_SECRET), config.get(IDENTITY_SECRET)
    if not isinstance(stream_secret, str) or len(stream_secret.encode("utf-8")) < 32:
        raise WhatsAppProviderPolicyError("meta_provider_stream_secret_missing")
    if not isinstance(identity_secret, str) or len(identity_secret.encode("utf-8")) < 32:
        raise WhatsAppProviderPolicyError("meta_provider_identity_secret_missing")
    version = config.get(IDENTITY_VERSION)
    if not isinstance(version, str) or not re.fullmatch(r"meta-[a-z0-9_.:-]{1,27}", version):
        raise WhatsAppProviderPolicyError("meta_provider_identity_version_invalid")
    tenants = config.get(QUEUE_TENANTS)
    if not isinstance(tenants, str) or not tenants.strip():
        raise WhatsAppProviderPolicyError("meta_provider_tenant_allowlist_required")
    result = dict(config)
    result.update(
        WHATSAPP_INBOUND_DURABILITY_MODE="queue",
        WHATSAPP_INBOUND_QUEUE_TENANT_IDS=tenants,
        WHATSAPP_INBOUND_HASH_SECRET=stream_secret,
        CHANNEL_SESSION_IDENTITY_MODE="enforce",
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=identity_secret,
        CHANNEL_SESSION_IDENTITY_VERSION_V1=version,
    )
    return result
