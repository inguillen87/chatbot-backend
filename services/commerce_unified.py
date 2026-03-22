from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from services.commerce_contracts import build_contact_key, normalize_sales_channel


_COMMERCIAL_STAGE_BY_STATUS = {
    "open": "cart_active",
    "submitted": "checkout_submitted",
    "pending": "awaiting_confirmation",
    "pendiente": "awaiting_confirmation",
    "pending_payment": "awaiting_payment",
    "pendiente_pago": "awaiting_payment",
    "confirmed": "confirmed",
    "confirmado": "confirmed",
    "paid": "paid",
    "pagado": "paid",
    "processing": "fulfillment",
    "en_proceso": "fulfillment",
    "preparing": "fulfillment",
    "shipped": "in_transit",
    "enviado": "in_transit",
    "delivered": "completed",
    "entregado": "completed",
    "completed": "completed",
    "completado": "completed",
    "cancelled": "cancelled",
    "cancelado": "cancelled",
    "returned": "post_sale",
    "devuelto": "post_sale",
}


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def _derive_stage(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    return _COMMERCIAL_STAGE_BY_STATUS.get(normalized, "in_progress")


def _normalize_contact(*, name: Any = None, email: Any = None, phone: Any = None, user_id: Any = None, anon_id: Any = None, session_id: Any = None) -> dict[str, Any]:
    return {
        "name": str(name).strip() if name else None,
        "email": str(email).strip().lower() if email else None,
        "phone": str(phone).strip() if phone else None,
        "contact_key": build_contact_key(
            user_id=user_id,
            email=email,
            phone=phone,
            anon_id=anon_id,
            session_id=session_id,
        ),
    }


def _legacy_items(detalles: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(detalles or "[]")
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return []

    items: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        qty = item.get("cantidad") or item.get("quantity") or 1
        items.append(
            {
                "sku": item.get("sku"),
                "title": item.get("nombre") or item.get("nombre_producto") or item.get("title"),
                "quantity": int(qty) if str(qty).isdigit() else 1,
                "unit_price": _as_float(item.get("precio_unitario") or item.get("precio")),
                "subtotal": _as_float(item.get("subtotal")),
                "currency": item.get("currency_id") or "ARS",
            }
        )
    return items


def serialize_unified_order(record: Any) -> dict[str, Any]:
    from models import MarketOrder, Order, PedidoConversacional, PymePedido

    if isinstance(record, MarketOrder):
        contact = _normalize_contact(
            name=record.contact_name,
            email=record.contact_email,
            phone=record.contact_phone,
            user_id=record.user_id,
            session_id=record.session_id,
        )
        items = [
            {
                "id": item.id,
                "product_id": item.product_id,
                "title": item.name_snapshot,
                "quantity": item.quantity,
                "unit_price": _as_float(item.price_monetary),
                "points": item.price_points,
                "currency": item.currency,
                "modalidad": item.modalidad,
            }
            for item in record.items
        ]
        return {
            "id": f"market:{record.id}",
            "source_model": "MarketOrder",
            "source_id": record.id,
            "tenant_id": record.tenant_id,
            "status": record.status,
            "commercial_stage": _derive_stage(record.status),
            "channel": normalize_sales_channel(record.channel),
            "contact": contact,
            "customer_profile": {
                **contact,
                "user_id": record.user_id,
                "session_id": record.session_id,
            },
            "totals": {
                "monetary": _as_float(record.total_monetary) or 0.0,
                "points": record.total_points or 0,
                "currency": record.currency or "ARS",
            },
            "items": items,
            "external_refs": {
                "provider": record.external_provider,
                "order_id": record.external_order_id,
                "url": record.external_url,
            },
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "metadata": record.metadata_payload or {},
        }

    if isinstance(record, PedidoConversacional):
        metadata = record.metadata_payload or {}
        contacto = metadata.get("contacto") if isinstance(metadata.get("contacto"), dict) else {}
        contact = _normalize_contact(
            name=contacto.get("nombre"),
            email=contacto.get("email"),
            phone=contacto.get("telefono"),
            user_id=record.user_id,
            anon_id=record.anon_id,
        )
        return {
            "id": f"conversational:{record.id}",
            "source_model": "PedidoConversacional",
            "source_id": record.id,
            "tenant_id": record.tenant_id,
            "status": record.estado,
            "commercial_stage": _derive_stage(record.estado),
            "channel": normalize_sales_channel(record.origen),
            "contact": {
                **contact,
                "contact_key": metadata.get("contact_key") or contact.get("contact_key"),
            },
            "customer_profile": {
                **contact,
                "contact_key": metadata.get("contact_key") or contact.get("contact_key"),
                "user_id": record.user_id,
                "anon_id": record.anon_id,
            },
            "totals": {
                "monetary": _as_float(record.monto_monetario) or 0.0,
                "points": record.monto_puntos or 0,
                "currency": metadata.get("currency") or "ARS",
            },
            "items": record.items or [],
            "external_refs": {
                "mp_preference_id": record.mp_preference_id,
                "mp_payment_id": record.mp_payment_id,
                "mp_status": record.mp_status,
            },
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "metadata": metadata,
        }

    if isinstance(record, PymePedido):
        contact = _normalize_contact(
            name=record.nombre_cliente,
            email=record.email_cliente,
            phone=record.telefono_cliente,
            user_id=record.user_id,
        )
        metadata = {
            "direccion": record.direccion,
            "pyme_id": record.pyme_id,
            "tenant_id": record.tenant_id,
            "rubro": getattr(record, "rubro", None),
        }
        return {
            "id": f"legacy:{record.id}",
            "source_model": "PymePedido",
            "source_id": record.id,
            "legacy_number": record.nro_pedido,
            "tenant_id": record.tenant_id,
            "status": record.estado,
            "commercial_stage": _derive_stage(record.estado),
            "channel": normalize_sales_channel(getattr(record, "channel", None) or "whatsapp"),
            "contact": contact,
            "customer_profile": {
                **contact,
                "user_id": record.user_id,
            },
            "totals": {
                "monetary": _as_float(record.monto_total) or 0.0,
                "points": 0,
                "currency": "ARS",
            },
            "items": _legacy_items(record.detalles),
            "external_refs": {
                "nro_pedido": record.nro_pedido,
            },
            "created_at": _iso(record.fecha),
            "updated_at": _iso(record.fecha),
            "metadata": metadata,
        }

    if isinstance(record, Order):
        contact = _normalize_contact(
            name=record.buyer_name,
            email=record.buyer_email,
            phone=record.buyer_phone,
            user_id=record.customer_id,
        )
        return {
            "id": f"order:{record.id}",
            "source_model": "Order",
            "source_id": record.id,
            "tenant_id": record.tenant_id,
            "status": record.status,
            "commercial_stage": _derive_stage(record.status),
            "channel": normalize_sales_channel(record.channel),
            "contact": contact,
            "customer_profile": {
                **contact,
                "user_id": record.customer_id,
            },
            "totals": {
                "monetary": _as_float(record.total) or 0.0,
                "points": 0,
                "currency": record.currency or "ARS",
            },
            "items": [item.to_dict() for item in record.items],
            "external_refs": {},
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "metadata": {
                "delivery_address": record.delivery_address,
            },
        }

    raise TypeError(f"Unsupported order record type: {type(record)!r}")
