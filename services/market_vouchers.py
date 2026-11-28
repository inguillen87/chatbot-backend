"""Vouchers for redemptions and free sign-ups: a code the person shows, a check the venue does.

When a WhatsApp order is sent, each redemption (school kit, medicine discount)
and each free sign-up (a talk, a transport) gets its own code. The person sees
it in the chat with a link to a page that shows it large and as a QR. The
venue (a shop, the neighbourhood centre) checks it from the panel: valid once,
then «ya usado».

Codes are 10 characters from an unambiguous alphabet (~50 bits) and only their
SHA-256 is stored, so the database never holds a usable code. They are kept in
``integration_event`` (provider ``chatboc_voucher``), whose unique key
(tenant, provider, event_id) gives an indexed lookup with no schema change.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from typing import Any

PROVIDER = "chatboc_voucher"
CONTRACT = "chatboc.market_voucher.v1"
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
_CODE = re.compile(r"[2-9A-HJKMNP-Z]{10}")
VOUCHER_MODALITIES = {"canje", "inscripcion"}


def new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))


def display(code: str) -> str:
    return f"{code[:4]}-{code[4:8]}-{code[8:]}"


def normalize(raw: Any) -> str | None:
    text = re.sub(r"[\s\-_.]", "", str(raw or "")).upper()
    return text if _CODE.fullmatch(text) else None


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_for_order(order: Any) -> list[dict[str, Any]]:
    """One voucher per redemption or sign-up line of a sent order. Returns the plain codes once."""
    from models import IntegrationEvent, db

    issued: list[dict[str, Any]] = []
    for line in order.items:
        if (line.modalidad or "") not in VOUCHER_MODALITIES:
            continue
        code = new_code()
        db.session.add(IntegrationEvent(
            tenant_id=int(order.tenant_id), provider=PROVIDER, event_id=_digest(code),
            event_type="voucher_issued", processed=False,
            payload={
                "order_id": order.id,
                "item_id": line.product_id,
                "name": str(line.name_snapshot or "Beneficio")[:120],
                "quantity": int(line.quantity or 1),
                "points": int(line.price_points or 0) * int(line.quantity or 1) or None,
                "kind": line.modalidad,
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
        ))
        issued.append({"code": code, "display": display(code), "name": line.name_snapshot,
                       "quantity": int(line.quantity or 1), "kind": line.modalidad})
    return issued


def _find(code: str, tenant_id: int | None = None, *, lock: bool = False) -> Any:
    from models import IntegrationEvent

    query = IntegrationEvent.query.filter_by(provider=PROVIDER, event_id=_digest(code))
    if tenant_id is not None:
        query = query.filter_by(tenant_id=int(tenant_id))
    if lock:
        # Two staff members checking the same code at once: only one marks it used.
        query = query.with_for_update()
    return query.first()


def _view(row: Any, code: str) -> dict[str, Any]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    tenant = row.tenant
    return {
        "contract_version": CONTRACT,
        "code": display(code),
        "status": "used" if row.processed else "valid",
        "name": payload.get("name"),
        "quantity": payload.get("quantity") or 1,
        "kind": payload.get("kind"),
        "points": payload.get("points"),
        "issued_at": payload.get("issued_at"),
        "used_at": payload.get("used_at"),
        "store": {"name": getattr(tenant, "nombre", None), "slug": getattr(tenant, "slug", None)},
    }


def public_view(raw_code: Any) -> dict[str, Any] | None:
    """What the person sees on their voucher page. The code itself is the key."""
    code = normalize(raw_code)
    row = _find(code) if code else None
    return _view(row, code) if row is not None else None


def redeem(tenant: Any, raw_code: Any, *, staff_user_id: int | None) -> tuple[str, dict[str, Any] | None]:
    """Mark a voucher of this organization as used. ('invalid'|'used'|'redeemed', view)."""
    from models import db

    code = normalize(raw_code)
    row = _find(code, tenant.id, lock=True) if code else None
    if row is None:
        return "invalid", None
    if row.processed:
        return "used", _view(row, code)
    payload = dict(row.payload or {})
    payload["used_at"] = datetime.now(timezone.utc).isoformat()
    payload["used_by"] = staff_user_id
    row.payload = payload
    row.processed = True
    row.event_type = "voucher_redeemed"
    db.session.commit()
    return "redeemed", _view(row, code)
