"""Opaque refetch events for authenticated operator rooms, never entity data."""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


def emit_commerce_invalidation(
    emit: Callable[..., Any], *, tenant_id: object, resource: str, event_name: str
) -> bool:
    """Emit a fixed allowlisted payload; an absent tenant never means broadcast.

    Only server-derived persisted tenant IDs belong here. Room membership is
    authorized by socket_service; clients must refetch via their HTTP permissions.
    Buyer notifications need a separate, verified identity/receipt subscription.
    """
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, (int, str)):
        return False
    normalized_id = str(tenant_id)
    if not re.fullmatch(r"[1-9][0-9]*", normalized_id):
        return False
    if resource not in {"orders", "payments"}:
        return False
    if event_name != "payment_update" and not (
        resource == "orders" and re.fullmatch(r"market_order_[A-Za-z0-9_-]{1,128}", event_name)
    ):
        return False
    emit(
        event_name,
        {"contract_version": "collections.invalidated.v1", "resource": resource,
         "reason": "collection_changed", "refetch": True},
        room=f"tenant_{normalized_id}",
    )
    return True
