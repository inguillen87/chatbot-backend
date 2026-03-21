from __future__ import annotations

from typing import Any


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_claim_confirmation_payload(*, categoria: Any = None, ubicacion: Any = None, descripcion: Any = None, nombre: Any = None, telefono: Any = None, email: Any = None, channel: str | None = None) -> dict[str, Any]:
    payload = {
        "categoria": _clean(categoria),
        "ubicacion": _clean(ubicacion),
        "descripcion": _clean(descripcion),
        "nombre": _clean(nombre),
        "telefono": _clean(telefono),
        "email": _clean(email),
        "channel": _clean(channel) or "web",
    }
    contact = payload["telefono"] or payload["email"] or payload["nombre"] or "sin contacto"
    parts = []
    if payload["categoria"]:
        parts.append(f"Categoría: {payload['categoria']}")
    if payload["ubicacion"]:
        parts.append(f"Ubicación: {payload['ubicacion']}")
    if payload["descripcion"]:
        parts.append(f"Detalle: {payload['descripcion']}")
    parts.append(f"Contacto: {contact}")
    payload["summary_text"] = " | ".join(parts)
    payload["summary_voice"] = (
        f"Confirmo categoría {payload['categoria'] or 'sin categoría'}, "
        f"ubicación {payload['ubicacion'] or 'sin ubicación'} "
        f"y contacto {contact}."
    )
    return payload


def build_order_confirmation_payload(*, cart_summary: dict | None = None, customer: dict | None = None, delivery_address: Any = None, channel: str | None = None) -> dict[str, Any]:
    cart_summary = cart_summary if isinstance(cart_summary, dict) else {}
    customer = customer if isinstance(customer, dict) else {}
    items = cart_summary.get("items_detalle") if isinstance(cart_summary.get("items_detalle"), list) else []
    top_items: list[str] = []
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        qty = item.get("cantidad")
        name = _clean(item.get("nombre_producto")) or _clean(item.get("nombre"))
        if not name:
            continue
        prefix = f"{qty} x " if qty else ""
        top_items.append(f"{prefix}{name}")
    customer_contact = _clean(customer.get("telefono")) or _clean(customer.get("email")) or _clean(customer.get("nombre")) or "sin contacto"
    address = _clean(delivery_address) or _clean(customer.get("direccion")) or "sin dirección"
    total = cart_summary.get("total_final_con_descuento")
    payload = {
        "items_preview": top_items,
        "items_count": len(items),
        "contact": customer_contact,
        "delivery_address": address,
        "channel": _clean(channel) or "web",
        "total": total,
    }
    total_text = f" | Total estimado: ${total:,.2f}" if isinstance(total, (int, float)) else ""
    payload["summary_text"] = (
        f"Pedido: {', '.join(top_items) if top_items else 'sin items'} | "
        f"Entrega: {address} | Contacto: {customer_contact}{total_text}"
    )
    payload["summary_voice"] = (
        f"Confirmo pedido con {len(items)} item{'s' if len(items) != 1 else ''}, "
        f"entrega en {address} y contacto {customer_contact}."
    )
    return payload
