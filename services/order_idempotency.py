"""Canonical payload binding for tenant-scoped PymePedido idempotency.

Only a SHA-256 digest is persisted.  The canonical payload deliberately
contains the durable order fields (including contact and delivery data) but
this module never logs or stores their plaintext outside ``PymePedido``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


ORDER_IDEMPOTENCY_CONTRACT_VERSION = "pyme.order.idempotency.v1"


def _clean_text(value: Any, *, lowercase: bool = False) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.lower() if lowercase else normalized


def _canonical_number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None if value is None else str(value).lower()
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return _clean_text(value)
    if not number.is_finite():
        return _clean_text(value)
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(value[key])
            for key in sorted(value, key=lambda candidate: str(candidate))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value)
    return value if isinstance(value, str) else str(value)


def _canonical_details(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {"raw": value.strip()}
    else:
        parsed = value
    return _canonical_json_value(parsed)


def _currency_from_details(details: Any) -> str | None:
    canonical = _canonical_details(details)
    if not isinstance(canonical, list):
        return None
    for item in canonical:
        if not isinstance(item, Mapping):
            continue
        for key in ("moneda", "currency_id", "currency"):
            currency = _clean_text(item.get(key))
            if currency:
                return currency.upper()
    return None


def canonical_order_payload(
    payload: Mapping[str, Any],
    *,
    tenant_id: Any | None = None,
    pyme_id: Any | None = None,
) -> dict[str, Any]:
    """Return the stable, privacy-preserving input to the order digest."""

    detalles = payload.get("detalles")
    currency = _clean_text(payload.get("moneda") or payload.get("currency"))
    return {
        "contract_version": ORDER_IDEMPOTENCY_CONTRACT_VERSION,
        "tenant_id": _canonical_number(
            tenant_id if tenant_id is not None else payload.get("tenant_id")
        ),
        "pyme_id": _canonical_number(
            pyme_id if pyme_id is not None else payload.get("pyme_id")
        ),
        "user_id": _canonical_number(payload.get("user_id")),
        "asunto": _clean_text(payload.get("asunto")),
        "detalles": _canonical_details(detalles),
        "monto_total": _canonical_number(payload.get("monto_total")),
        "moneda": currency.upper() if currency else _currency_from_details(detalles),
        "nombre_cliente": _clean_text(payload.get("nombre_cliente")),
        "email_cliente": _clean_text(payload.get("email_cliente"), lowercase=True),
        "telefono_cliente": _clean_text(payload.get("telefono_cliente")),
        "direccion": _clean_text(payload.get("direccion")),
        "latitud": _canonical_number(payload.get("latitud")),
        "longitud": _canonical_number(payload.get("longitud")),
        "estado": (_clean_text(payload.get("estado")) or "pendiente").lower(),
    }


def order_payload_hash(
    payload: Mapping[str, Any],
    *,
    tenant_id: Any | None = None,
    pyme_id: Any | None = None,
) -> str:
    canonical = canonical_order_payload(
        payload,
        tenant_id=tenant_id,
        pyme_id=pyme_id,
    )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def order_payload_hash_from_record(order: Any) -> str:
    return order_payload_hash(
        {
            "tenant_id": getattr(order, "tenant_id", None),
            "pyme_id": getattr(order, "pyme_id", None),
            "user_id": getattr(order, "user_id", None),
            "asunto": getattr(order, "asunto", None),
            "detalles": getattr(order, "detalles", None),
            "monto_total": getattr(order, "monto_total", None),
            "moneda": getattr(order, "moneda", None),
            "nombre_cliente": getattr(order, "nombre_cliente", None),
            "email_cliente": getattr(order, "email_cliente", None),
            "telefono_cliente": getattr(order, "telefono_cliente", None),
            "direccion": getattr(order, "direccion", None),
            "latitud": getattr(order, "latitud", None),
            "longitud": getattr(order, "longitud", None),
            "estado": getattr(order, "estado", None),
        }
    )


def existing_order_payload_matches(order: Any, expected_hash: str) -> tuple[bool, bool]:
    """Return ``(matches, needs_legacy_backfill)`` without exposing payloads."""

    stored_hash = _clean_text(getattr(order, "idempotency_payload_hash", None))
    if stored_hash:
        return hmac.compare_digest(stored_hash, expected_hash), False
    reconstructed_hash = order_payload_hash_from_record(order)
    return hmac.compare_digest(reconstructed_hash, expected_hash), True
