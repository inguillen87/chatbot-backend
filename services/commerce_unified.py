from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

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


def _record_timestamp(order: dict[str, Any]) -> str:
    return str(order.get("updated_at") or order.get("created_at") or "")


def _dedupe_key(order: dict[str, Any]) -> str:
    source_model = str(order.get("source_model") or "")
    external_refs = order.get("external_refs") if isinstance(order.get("external_refs"), dict) else {}
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}

    if source_model == "MarketOrder" and metadata.get("source_conversational_id"):
        return f"conv:{metadata.get('source_conversational_id')}"
    if source_model == "MarketOrder" and external_refs.get("provider") == "pedido_conversacional" and external_refs.get("order_id"):
        return f"conv:{external_refs.get('order_id')}"
    if source_model == "PedidoConversacional" and order.get("source_id") is not None:
        return f"conv:{order.get('source_id')}"
    if source_model == "PymePedido":
        idempotency_key = str(metadata.get("idempotency_key") or external_refs.get("idempotency_key") or "")
        if idempotency_key.startswith("conv_order_"):
            return f"conv:{idempotency_key.split('conv_order_', 1)[1]}"
    return f"{source_model}:{order.get('source_id')}"


def _dedupe_priority(order: dict[str, Any]) -> int:
    source_model = str(order.get("source_model") or "")
    external_refs = order.get("external_refs") if isinstance(order.get("external_refs"), dict) else {}
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}

    if source_model == "MarketOrder" and metadata.get("source_conversational_id"):
        return 4
    if source_model == "MarketOrder" and external_refs.get("provider") == "pedido_conversacional":
        return 4
    if source_model == "PedidoConversacional":
        return 3
    if source_model == "PymePedido" and str(metadata.get("idempotency_key") or "").startswith("conv_order_"):
        return 2
    return 1


def dedupe_unified_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for order in orders:
        key = _dedupe_key(order)
        current = deduped.get(key)
        if current is None:
            deduped[key] = order
            continue

        candidate_rank = (_dedupe_priority(order), _record_timestamp(order))
        current_rank = (_dedupe_priority(current), _record_timestamp(current))
        if candidate_rank > current_rank:
            deduped[key] = order

    return list(deduped.values())


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
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        qty = item.get("cantidad") or item.get("quantity") or 1
        items.append(
            _normalize_unified_item(
                {
                "sku": item.get("sku"),
                "name": item.get("nombre") or item.get("nombre_producto") or item.get("title"),
                "quantity": int(qty) if str(qty).isdigit() else 1,
                "price": _as_float(item.get("precio_unitario") or item.get("precio")),
                "subtotal": _as_float(item.get("subtotal")),
                "currency": item.get("currency_id") or "ARS",
                },
                index=index,
            )
        )
    return items


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _assisted_row_label(row: Any) -> str:
    if isinstance(row, dict):
        pieces = [
            str(part).strip()
            for part in (
                row.get("cantidad") or row.get("qty") or row.get("quantity"),
                row.get("nombre") or row.get("producto") or row.get("descripcion") or row.get("detalle") or row.get("item"),
                row.get("sku"),
            )
            if part
        ]
        return " ".join(pieces) or str(row)
    return str(row)


def _catalog_candidates_from_unmatched_rows(unmatched_rows: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for row in unmatched_rows:
        if not isinstance(row, dict):
            continue
        candidates = _as_list(row.get("catalog_candidates")) or _as_list(row.get("candidates"))
        if not candidates:
            continue
        row_without_candidates = {
            key: value
            for key, value in row.items()
            if key not in {"catalog_candidates", "candidates"}
        }
        payload.append(
            {
                "item": _assisted_row_label(row_without_candidates),
                "row": row_without_candidates,
                "candidates": candidates,
            }
        )
    return payload


def _normalize_unified_item(item: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    quantity = item.get("quantity") or item.get("cantidad") or item.get("qty") or 1
    unit_price = item.get("unit_price")
    if unit_price is None:
        unit_price = item.get("price")
    if unit_price is None:
        unit_price = item.get("precio_float") or item.get("precio_unitario") or item.get("precio")
    normalized_price = _as_float(unit_price) or 0.0
    try:
        normalized_quantity = int(quantity)
    except (TypeError, ValueError):
        normalized_quantity = 1
    title = item.get("name") or item.get("title") or item.get("nombre") or item.get("nombre_producto") or item.get("sku")
    return {
        "id": item.get("id") or item.get("catalogo_item_id") or item.get("product_id") or f"item-{index + 1}",
        "product_id": item.get("product_id") or item.get("catalogo_item_id") or item.get("id"),
        "sku": item.get("sku"),
        "name": title or "Articulo detectado",
        "title": title or "Articulo detectado",
        "quantity": normalized_quantity,
        "price": normalized_price,
        "unit_price": normalized_price,
        "subtotal": _as_float(item.get("subtotal")) or (normalized_price * normalized_quantity),
        "currency": item.get("currency") or item.get("currency_id") or "ARS",
    }


def _digits_only(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _operator_task(task_id: str, label: str, description: str, *, tone: str = "neutral") -> dict[str, str]:
    return {"id": task_id, "label": label, "description": description, "tone": tone}


def _build_assisted_operator_pack(
    *,
    record_id: Any = None,
    request_kind_label: str | None,
    contact: dict[str, Any],
    match_summary: dict[str, Any],
    unmatched_items: list[Any],
    extraction_error: Any = None,
    primary_intent: str | None = None,
) -> dict[str, Any]:
    matched = int(match_summary.get("matched") or 0)
    unmatched = int(match_summary.get("unmatched") or 0)
    detected = int(match_summary.get("detected") or (matched + unmatched))
    name = str(contact.get("name") or contact.get("nombre") or "cliente").strip()
    phone = str(contact.get("phone") or contact.get("telefono") or contact.get("whatsapp") or "").strip()
    email = str(contact.get("email") or "").strip()
    ref = f"pedido:{record_id}" if record_id else None
    label = request_kind_label or "pedido"
    intent = str(primary_intent or "").strip()
    is_service_request = intent == "municipal_service_request"
    is_document_review = intent in {
        "document_review",
        "tax_or_payment_support",
        "certificate_or_procedure_review",
        "manual_review",
    }

    tasks: list[dict[str, str]] = []
    if extraction_error:
        tasks.append(
            _operator_task(
                "review_ocr_confidence",
                "Revisar lectura IA",
                "La extraccion llego con baja confianza. Abrir archivo/texto original antes de cotizar.",
                tone="warning",
            )
        )
    if is_service_request:
        tasks.append(
            _operator_task(
                "classify_area_and_location",
                "Derivar area y validar ubicacion",
                "Revisar direccion, categoria y urgencia antes de crear o asignar el ticket municipal.",
                tone="primary",
            )
        )
    if unmatched:
        if is_service_request:
            unmatched_description = (
                "Completar direccion, categoria, descripcion o contacto para los puntos que la IA no leyo con seguridad."
            )
        elif is_document_review:
            unmatched_description = (
                "Completar cuenta, periodo, vencimiento, titular, tramite o dato documental que la IA no leyo con seguridad."
            )
        else:
            unmatched_description = "Completar SKU, precio, stock o alternativa para los renglones que no matchearon con catalogo."
        tasks.append(
            _operator_task(
                "resolve_unmatched_items",
                "Resolver datos no interpretados",
                unmatched_description,
                tone="warning",
            )
        )
    if matched and not is_service_request and not is_document_review:
        tasks.append(
            _operator_task(
                "confirm_stock_price",
                "Confirmar stock y precio",
                "Validar disponibilidad, promocion vigente y condiciones antes de responder al cliente.",
                tone="success",
            )
        )
    if not phone and not email:
        tasks.append(
            _operator_task(
                "request_contact",
                "Pedir dato de contacto",
                "No hay WhatsApp ni email cargado. Responder desde el canal de origen o solicitar contacto.",
                tone="warning",
            )
        )
    tasks.append(
        _operator_task(
            "send_customer_reply",
            "Responder al vecino" if is_service_request else "Enviar respuesta al cliente",
            (
                "Usar el mensaje sugerido como base, confirmar recepcion, estado y proximo paso del area."
                if is_service_request
                else "Usar el mensaje sugerido como base y continuar la venta, cotizacion o revision operativa."
            ),
            tone="primary",
        )
    )

    priority = "high" if extraction_error or unmatched or (not phone and not email) else "normal"
    reply_parts = [
        f"Hola {name}, recibimos tu {label}.",
    ]
    if is_service_request:
        reply_parts.append(
            f"Detectamos {detected} punto(s) para revisar y derivar al area correspondiente."
        )
    elif is_document_review:
        reply_parts.append(
            f"Detectamos {detected} dato(s) para validar antes de responder el proximo paso."
        )
    else:
        reply_parts.append(
            f"Detectamos {detected} renglon(es): {matched} asociado(s) al catalogo y {unmatched} para revisar."
        )
    if unmatched_items:
        preview = ", ".join(str(item) for item in unmatched_items[:4])
        reply_parts.append(f"Estamos revisando estos datos: {preview}.")
    if ref:
        reply_parts.append(f"Referencia interna: {ref}.")
    reply_parts.append(
        (
            "Te respondemos por este canal con estado, area responsable y proximo paso."
            if is_service_request
            else (
                "Te respondemos por este canal con la validacion y proximo paso."
                if is_document_review
                else "Te respondemos por este canal con stock, precio y proximo paso."
            )
        )
    )
    suggested_reply = " ".join(reply_parts)

    contact_links = []
    digits = _digits_only(phone)
    if digits:
        contact_links.append(
            {
                "type": "whatsapp",
                "label": "Responder por WhatsApp",
                "href": f"https://wa.me/{digits}?text={quote(suggested_reply)}",
            }
        )
    if email:
        contact_links.append(
            {
                "type": "email",
                "label": "Responder por email",
                "href": f"mailto:{email}?subject={quote('Seguimiento de tu solicitud')}&body={quote(suggested_reply)}",
            }
        )

    return {
        "priority": priority,
        "reference": ref,
        "primary_intent": intent or None,
        "suggested_reply": suggested_reply,
        "suggested_tasks": tasks,
        "contact_links": contact_links,
        "needs_human_review": priority == "high" or bool(match_summary.get("needs_operator_review")),
    }


def _build_assisted_request(metadata: dict[str, Any], raw_items: list[Any], *, record_id: Any = None) -> dict[str, Any] | None:
    mode = metadata.get("mode")
    contract_version = metadata.get("contract_version")
    if contract_version != "marketplace.assisted_request.v1" and mode != "order_note_upload":
        return None

    raw_payload = _as_dict(raw_items[0]) if raw_items else {}
    detected = _as_list(raw_payload.get("items_detectados"))
    unmatched_rows = _as_list(raw_payload.get("no_encontrados"))
    unmatched_labels = _as_list(raw_payload.get("no_encontrados_labels"))
    if not unmatched_labels and unmatched_rows:
        unmatched_labels = [
            str(row.get("nombre") or row.get("sku") or row.get("descripcion") or row.get("detalle") or row)
            for row in unmatched_rows
            if isinstance(row, dict) or row
        ]
    catalog_candidates = (
        _as_list(metadata.get("catalog_candidates"))
        or _as_list(raw_payload.get("catalog_candidates"))
        or _catalog_candidates_from_unmatched_rows(unmatched_rows)
    )

    raw_contact = _as_dict(metadata.get("contact")) or _as_dict(raw_payload.get("contact"))
    legacy_contact = _as_dict(metadata.get("contacto")) or _as_dict(raw_payload.get("contacto"))
    assisted_contact = raw_contact or {
        "name": legacy_contact.get("nombre") or legacy_contact.get("name"),
        "phone": legacy_contact.get("telefono") or legacy_contact.get("phone"),
        "email": legacy_contact.get("email"),
        "notes": legacy_contact.get("observaciones") or legacy_contact.get("notes"),
    }
    assisted_contact = {key: value for key, value in assisted_contact.items() if value}
    source = _as_dict(metadata.get("source")) or {
        "archivo_url": raw_payload.get("archivo_url"),
        "archivo_nombre": raw_payload.get("archivo_nombre"),
    }
    match_summary = _as_dict(metadata.get("match_summary")) or raw_payload.get("match_summary") or {}
    extraction_error = metadata.get("extraction_error") or _as_dict(metadata.get("source")).get("extraction_error")
    request_kind_label = metadata.get("request_kind_label") or raw_payload.get("request_kind_label")
    metadata_crm_handoff = _as_dict(metadata.get("crm_handoff"))
    raw_crm_handoff = _as_dict(raw_payload.get("crm_handoff"))
    crm_handoff = dict(metadata_crm_handoff or raw_crm_handoff)
    crm_order_draft = (
        _as_dict(metadata.get("crm_order_draft"))
        or _as_dict(raw_payload.get("crm_order_draft"))
        or _as_dict(metadata_crm_handoff.get("draft_order"))
        or _as_dict(raw_crm_handoff.get("draft_order"))
    )
    if crm_order_draft:
        crm_order_draft = dict(crm_order_draft)
        crm_handoff = {**crm_handoff, "draft_order": crm_order_draft}
    persisted_operator_pack = _as_dict(metadata.get("operator_pack")) or _as_dict(raw_payload.get("operator_pack"))
    if persisted_operator_pack and persisted_operator_pack.get("suggested_reply"):
        operator_pack = dict(persisted_operator_pack)
        if record_id and not operator_pack.get("reference"):
            operator_pack["reference"] = f"pedido:{record_id}"
    else:
        operator_pack = _build_assisted_operator_pack(
            record_id=record_id,
            request_kind_label=request_kind_label,
            contact=assisted_contact,
            match_summary=match_summary,
            unmatched_items=unmatched_labels,
            extraction_error=extraction_error,
            primary_intent=(_as_dict(metadata.get("document_profile")) or _as_dict(raw_payload.get("document_profile"))).get("primary_intent"),
        )

    return {
        "contract_version": contract_version or "marketplace.assisted_request.v1",
        "mode": mode or "order_note_upload",
        "crm_state": metadata.get("crm_state") or "pending_operator_review",
        "request_kind": metadata.get("request_kind") or raw_payload.get("request_kind"),
        "request_kind_label": request_kind_label,
        "document_profile": _as_dict(metadata.get("document_profile")) or _as_dict(raw_payload.get("document_profile")),
        "structured_extraction": _as_dict(metadata.get("structured_extraction")) or _as_dict(raw_payload.get("structured_extraction")),
        "crm_handoff": crm_handoff,
        "crm_order_draft": crm_order_draft or None,
        "contact": assisted_contact,
        "source": source,
        "match_summary": match_summary,
        "review_context": _as_dict(metadata.get("review_context")) or _as_dict(raw_payload.get("review_context")),
        "row_errors": _as_list(metadata.get("row_errors")) or _as_list(raw_payload.get("row_errors")),
        "extraction_error": extraction_error,
        "next_actions": _as_list(metadata.get("next_actions")),
        "public_follow_up": _as_dict(metadata.get("public_follow_up")) or _as_dict(raw_payload.get("public_follow_up")),
        "intake_experience": _as_dict(metadata.get("intake_experience")) or _as_dict(raw_payload.get("intake_experience")),
        "operator_pack": operator_pack,
        "operator_intake_summary": _as_dict(metadata.get("operator_intake_summary")) or _as_dict(raw_payload.get("operator_intake_summary")),
        "customer_message": raw_payload.get("customer_message") or metadata.get("customer_message"),
        "customer_next_steps": _as_list(metadata.get("customer_next_steps")) or _as_list(raw_payload.get("customer_next_steps")),
        "detected_items": [
            _normalize_unified_item(item, index=index)
            for index, item in enumerate(detected)
            if isinstance(item, dict)
        ],
        "unmatched_items": unmatched_labels,
        "raw_unmatched_rows": unmatched_rows,
        "catalog_candidates": catalog_candidates,
    }


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
            _normalize_unified_item({
                "id": item.id,
                "product_id": item.product_id,
                "name": item.name_snapshot,
                "quantity": item.quantity,
                "price": _as_float(item.price_monetary),
                "points": item.price_points,
                "currency": item.currency,
                "modalidad": item.modalidad,
            }, index=index)
            for index, item in enumerate(record.items)
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
            "total": _as_float(record.total_monetary) or 0.0,
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
        raw_items = record.items or []
        assisted_request = _build_assisted_request(metadata, raw_items, record_id=record.id)
        normalized_items = (
            assisted_request.get("detected_items", []) if assisted_request else [
                _normalize_unified_item(item, index=index)
                for index, item in enumerate(raw_items)
                if isinstance(item, dict)
            ]
        )
        raw_contact = metadata.get("contact") if isinstance(metadata.get("contact"), dict) else {}
        legacy_contact = metadata.get("contacto") if isinstance(metadata.get("contacto"), dict) else {}
        contacto = raw_contact or legacy_contact
        contact = _normalize_contact(
            name=contacto.get("name") or contacto.get("nombre"),
            email=contacto.get("email"),
            phone=contacto.get("phone") or contacto.get("telefono"),
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
            "total": _as_float(record.monto_monetario) or 0.0,
            "items": normalized_items,
            "external_refs": {
                "mp_preference_id": record.mp_preference_id,
                "mp_payment_id": record.mp_payment_id,
                "mp_status": record.mp_status,
            },
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "assisted_request": assisted_request,
            "metadata": {
                **metadata,
                "idempotency_key": getattr(record, "idempotency_key", None),
            },
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
            "total": _as_float(record.monto_total) or 0.0,
            "items": _legacy_items(record.detalles),
            "external_refs": {
                "nro_pedido": record.nro_pedido,
                "idempotency_key": getattr(record, "idempotency_key", None),
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
            "total": _as_float(record.total) or 0.0,
            "items": [
                _normalize_unified_item(item.to_dict(), index=index)
                for index, item in enumerate(record.items)
            ],
            "external_refs": {},
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "metadata": {
                "delivery_address": record.delivery_address,
            },
        }

    raise TypeError(f"Unsupported order record type: {type(record)!r}")
