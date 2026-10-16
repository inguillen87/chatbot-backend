"""Shared safety contract for provider-connection cutover tooling."""

from __future__ import annotations

import hashlib


ADVISORY_LOCK_NAMESPACE = "chatboc.provider-connection-cutover-lock.v1"
MANAGED_CONNECTION_MARKER = "cutover_managed_provider_connection"
MANAGED_CONNECTION_CONTRACT_VERSION = (
    "chatboc.tenant_provider_connection_reconciliation.v1"
)


def advisory_lock_keys(
    *,
    database_identity_sha256: str,
    tenant_slug: str,
    external_account_id: str,
) -> tuple[int, ...]:
    prefix = f"{ADVISORY_LOCK_NAMESPACE}:{database_identity_sha256}:"
    materials = {
        f"{prefix}tenant:{tenant_slug}",
        f"{prefix}external-account:{external_account_id}",
    }
    keys = []
    for material in materials:
        key = int.from_bytes(
            hashlib.sha256(material.encode("utf-8")).digest()[:8], "big"
        )
        keys.append(key & ((1 << 63) - 1) or 1)
    return tuple(sorted(keys))
