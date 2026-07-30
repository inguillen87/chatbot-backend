"""Fail-closed tenant canary policy for the generic domain-effect outbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class DomainEffectOutboxConfigurationError(RuntimeError):
    """Raised when queue mode cannot safely stage durable effects."""


@dataclass(frozen=True)
class DomainEffectOutboxPolicy:
    mode: str
    tenant_id: int
    enabled: bool
    secret: str | None
    max_payload_bytes: int
    max_attempts: int


def _config_value(config: Mapping[str, Any] | Any, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    if not callable(getter):
        raise DomainEffectOutboxConfigurationError("domain_effect_config_unavailable")
    return getter(key, default)


def _bounded_int(
    config: Mapping[str, Any] | Any,
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = _config_value(config, key, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectOutboxConfigurationError(f"{key.lower()}_invalid") from exc
    if value < minimum or value > maximum:
        raise DomainEffectOutboxConfigurationError(f"{key.lower()}_out_of_range")
    return value


def _tenant_canary_ids(raw_value: Any) -> frozenset[int]:
    tokens = [token.strip() for token in str(raw_value or "").split(",") if token.strip()]
    if not tokens:
        raise DomainEffectOutboxConfigurationError("domain_effect_tenant_canary_required")

    tenant_ids: set[int] = set()
    for token in tokens:
        try:
            tenant_id = int(token)
        except (TypeError, ValueError) as exc:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_tenant_canary_invalid"
            ) from exc
        if tenant_id <= 0:
            raise DomainEffectOutboxConfigurationError(
                "domain_effect_tenant_canary_invalid"
            )
        tenant_ids.add(tenant_id)
    return frozenset(tenant_ids)


def resolve_domain_effect_outbox_policy(
    config: Mapping[str, Any] | Any,
    *,
    tenant_id: int,
) -> DomainEffectOutboxPolicy:
    """Resolve whether one tenant must stage effects instead of direct sends.

    Queue mode never silently falls back to legacy behavior for a canary tenant.
    Invalid queue configuration raises before the domain transaction commits.
    """

    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainEffectOutboxConfigurationError("domain_effect_tenant_invalid") from exc
    if normalized_tenant_id <= 0:
        raise DomainEffectOutboxConfigurationError("domain_effect_tenant_invalid")

    mode = str(
        _config_value(config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy") or "legacy"
    ).strip().lower()
    max_payload_bytes = _bounded_int(
        config,
        "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES",
        default=4096,
        minimum=256,
        maximum=8192,
    )
    max_attempts = _bounded_int(
        config,
        "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS",
        default=8,
        minimum=1,
        maximum=32,
    )

    if mode == "legacy":
        return DomainEffectOutboxPolicy(
            mode=mode,
            tenant_id=normalized_tenant_id,
            enabled=False,
            secret=None,
            max_payload_bytes=max_payload_bytes,
            max_attempts=max_attempts,
        )
    if mode != "queue":
        raise DomainEffectOutboxConfigurationError("domain_effect_mode_invalid")

    secret = str(_config_value(config, "DOMAIN_EFFECT_OUTBOX_SECRET", "") or "")
    if len(secret.encode("utf-8")) < 32:
        raise DomainEffectOutboxConfigurationError("domain_effect_secret_invalid")
    canary_ids = _tenant_canary_ids(
        _config_value(config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", "")
    )
    return DomainEffectOutboxPolicy(
        mode=mode,
        tenant_id=normalized_tenant_id,
        enabled=normalized_tenant_id in canary_ids,
        secret=secret if normalized_tenant_id in canary_ids else None,
        max_payload_bytes=max_payload_bytes,
        max_attempts=max_attempts,
    )


def resolve_domain_effect_outbox_canaries(
    config: Mapping[str, Any] | Any,
) -> frozenset[int]:
    """Return the explicit worker scope, validating queue credentials first."""

    mode = str(
        _config_value(config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy") or "legacy"
    ).strip().lower()
    if mode == "legacy":
        return frozenset()
    if mode != "queue":
        raise DomainEffectOutboxConfigurationError("domain_effect_mode_invalid")
    secret = str(_config_value(config, "DOMAIN_EFFECT_OUTBOX_SECRET", "") or "")
    if len(secret.encode("utf-8")) < 32:
        raise DomainEffectOutboxConfigurationError("domain_effect_secret_invalid")
    return _tenant_canary_ids(
        _config_value(config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", "")
    )


__all__ = [
    "DomainEffectOutboxConfigurationError",
    "DomainEffectOutboxPolicy",
    "resolve_domain_effect_outbox_canaries",
    "resolve_domain_effect_outbox_policy",
]
