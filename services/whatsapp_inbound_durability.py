"""Fail-closed tenant canary policy for durable WhatsApp inbound turns.

The allowlist scopes queue ingress and worker claims. It does not make
``CHANNEL_SESSION_IDENTITY_MODE=enforce`` tenant-specific; that prerequisite is
still process-wide and must be audited for every tenant before queue rollout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class WhatsAppInboundDurabilityConfigurationError(RuntimeError):
    """Raised when durable inbound processing cannot be scoped safely."""


@dataclass(frozen=True)
class WhatsAppInboundDurabilityPolicy:
    mode: str
    tenant_id: int | None
    queue_enabled: bool


def _config_value(config: Mapping[str, Any] | Any, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    if not callable(getter):
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_config_unavailable"
        )
    return getter(key, default)


def _queue_tenant_ids(raw_value: Any) -> frozenset[int]:
    tokens = [token.strip() for token in str(raw_value or "").split(",") if token.strip()]
    if not tokens:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_queue_tenant_allowlist_required"
        )

    tenant_ids: set[int] = set()
    for token in tokens:
        try:
            tenant_id = int(token)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WhatsAppInboundDurabilityConfigurationError(
                "whatsapp_inbound_queue_tenant_invalid"
            ) from exc
        if tenant_id <= 0:
            raise WhatsAppInboundDurabilityConfigurationError(
                "whatsapp_inbound_queue_tenant_invalid"
            )
        tenant_ids.add(tenant_id)
    return frozenset(tenant_ids)


def _queue_contract(config: Mapping[str, Any] | Any) -> frozenset[int]:
    secret = str(_config_value(config, "WHATSAPP_INBOUND_HASH_SECRET", "") or "")
    if len(secret.encode("utf-8")) < 32:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_hash_secret_invalid"
        )

    identity_mode = str(
        _config_value(config, "CHANNEL_SESSION_IDENTITY_MODE", "legacy") or "legacy"
    ).strip().lower()
    if identity_mode != "enforce":
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_session_identity_not_enforced"
        )
    identity_secret = str(
        _config_value(config, "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1", "") or ""
    )
    if len(identity_secret.encode("utf-8")) < 32:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_session_identity_secret_invalid"
        )

    return _queue_tenant_ids(
        _config_value(config, "WHATSAPP_INBOUND_QUEUE_TENANT_IDS", "")
    )


def resolve_whatsapp_inbound_durability_policy(
    config: Mapping[str, Any] | Any,
    *,
    tenant_id: Any,
) -> WhatsAppInboundDurabilityPolicy:
    """Resolve queue vs. legacy only from a trusted, canonical tenant id."""

    mode = str(
        _config_value(config, "WHATSAPP_INBOUND_DURABILITY_MODE", "legacy")
        or "legacy"
    ).strip().lower()
    if mode == "legacy" and tenant_id is None:
        # Older synchronous routes can resolve an authoritative owner without a
        # materialized TenantProfile.  No canary decision is made in legacy
        # mode, so rejecting that already-supported route would turn a dormant
        # rollout flag into a production outage.
        return WhatsAppInboundDurabilityPolicy(
            mode=mode,
            tenant_id=None,
            queue_enabled=False,
        )
    if mode not in {"legacy", "queue"}:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_durability_mode_invalid"
        )

    if isinstance(tenant_id, bool):
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_tenant_invalid"
        )
    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_tenant_invalid"
        ) from exc
    if normalized_tenant_id <= 0:
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_tenant_invalid"
        )

    if mode == "legacy":
        return WhatsAppInboundDurabilityPolicy(
            mode=mode,
            tenant_id=normalized_tenant_id,
            queue_enabled=False,
        )
    canaries = _queue_contract(config)
    return WhatsAppInboundDurabilityPolicy(
        mode=mode,
        tenant_id=normalized_tenant_id,
        queue_enabled=normalized_tenant_id in canaries,
    )


def resolve_whatsapp_inbound_queue_tenants(
    config: Mapping[str, Any] | Any,
) -> frozenset[int]:
    """Return the explicit worker scope after validating the queue contract."""

    mode = str(
        _config_value(config, "WHATSAPP_INBOUND_DURABILITY_MODE", "legacy")
        or "legacy"
    ).strip().lower()
    if mode == "legacy":
        return frozenset()
    if mode != "queue":
        raise WhatsAppInboundDurabilityConfigurationError(
            "whatsapp_inbound_durability_mode_invalid"
        )
    return _queue_contract(config)


__all__ = [
    "WhatsAppInboundDurabilityConfigurationError",
    "WhatsAppInboundDurabilityPolicy",
    "resolve_whatsapp_inbound_durability_policy",
    "resolve_whatsapp_inbound_queue_tenants",
]
