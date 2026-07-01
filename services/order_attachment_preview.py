from __future__ import annotations

from typing import Any

from services.commerce_contracts import normalize_sales_channel
from services.common_utils import parse_precio_flexible
from services.conversation_summaries import build_order_confirmation_payload
from services.llm_utils import extraer_lista_pedido_de_texto_con_llm
from services.pedido_processor_service import UMBRAL_SIMILITUD_OCR_PEDIDO, buscar_item_en_catalogo


_ASSISTED_REQUEST_CONTRACT_VERSION = "marketplace.assisted_request.v1"
_INTAKE_EXPERIENCE_CONTRACT_VERSION = "marketplace.assisted_intake_experience.v1"
_CRM_ORDER_DRAFT_CONTRACT_VERSION = "marketplace.crm_order_draft.v1"


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _build_contact_payload(
    *,
    telefono: Any = None,
    email: Any = None,
    nombre: Any = None,
    direccion: Any = None,
) -> dict[str, Any]:
    return {
        "phone": _clean_optional_text(telefono),
        "email": _clean_optional_text(email),
        "name": _clean_optional_text(nombre),
        "address": _clean_optional_text(direccion),
    }


def _quantity_value(value: Any) -> Any:
    if value is None or value == "":
        return 1
    return value


def _item_label(item: dict[str, Any]) -> str | None:
    return _clean_optional_text(
        item.get("nombre")
        or item.get("name")
        or item.get("title")
        or item.get("nombre_producto")
        or item.get("sku")
        or item.get("descripcion")
    )


def _catalog_match_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    catalog_match = item.get("catalog_match")
    if isinstance(catalog_match, dict) and catalog_match:
        return catalog_match
    catalog_item_id = item.get("catalogo_item_id") or item.get("catalog_item_id") or item.get("product_id")
    if not catalog_item_id:
        return None
    return {
        "catalogo_item_id": catalog_item_id,
        "product_id": catalog_item_id,
        "sku": item.get("sku"),
        "nombre": item.get("nombre") or item.get("name") or item.get("title"),
        "name": item.get("nombre") or item.get("name") or item.get("title"),
        "precio": item.get("precio") or item.get("precio_str"),
        "price": item.get("price") or item.get("precio_valor") or item.get("precio_float"),
        "moneda": item.get("moneda") or item.get("currency") or "ARS",
        "unidad": item.get("unidad") or item.get("unit"),
    }


def _candidate_count_for_unmatched(item: dict[str, Any], catalog_candidates: list[dict[str, Any]]) -> int:
    candidates = item.get("catalog_candidates") or item.get("candidates")
    if isinstance(candidates, list):
        return len(candidates)
    label = _item_label(item)
    if not label:
        return 0
    for group in catalog_candidates:
        if not isinstance(group, dict):
            continue
        row = group.get("row") if isinstance(group.get("row"), dict) else {}
        if group.get("item") == label or _item_label(row) == label:
            group_candidates = group.get("candidates")
            return len(group_candidates) if isinstance(group_candidates, list) else 0
    return 0


def build_crm_order_draft(
    *,
    request_kind: str = "order_note",
    request_kind_label: str = "nota de pedido",
    source: dict[str, Any] | None = None,
    contact: dict[str, Any] | None = None,
    matched_items: list[dict[str, Any]] | None = None,
    unmatched_items: list[Any] | None = None,
    catalog_candidates: list[dict[str, Any]] | None = None,
    match_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = source or {}
    contact = contact or {}
    matched_items = matched_items or []
    unmatched_items = unmatched_items or []
    catalog_candidates = catalog_candidates or []
    match_summary = match_summary or {}

    lines: list[dict[str, Any]] = []
    for item in matched_items:
        if not isinstance(item, dict):
            continue
        label = _item_label(item)
        if not label:
            continue
        catalog_match = _catalog_match_from_item(item)
        lines.append(
            {
                "line_id": f"line-{len(lines) + 1}",
                "status": "catalog_matched" if catalog_match else "needs_review",
                "source_name": label,
                "quantity": _quantity_value(item.get("cantidad") or item.get("quantity") or item.get("qty")),
                "unit": item.get("unidad") or item.get("unit"),
                "sku": item.get("sku") or (catalog_match or {}).get("sku"),
                "catalog_item_id": (catalog_match or {}).get("catalogo_item_id")
                or (catalog_match or {}).get("catalog_item_id")
                or (catalog_match or {}).get("product_id"),
                "catalog_match": catalog_match,
                "candidate_count": 0,
                "needs_operator_review": not bool(catalog_match),
            }
        )

    for raw_item in unmatched_items:
        if isinstance(raw_item, dict):
            label = _item_label(raw_item)
            quantity = _quantity_value(raw_item.get("cantidad") or raw_item.get("quantity") or raw_item.get("qty"))
            unit = raw_item.get("unidad") or raw_item.get("unit")
            sku = raw_item.get("sku")
            candidate_count = _candidate_count_for_unmatched(raw_item, catalog_candidates)
        else:
            label = _clean_optional_text(raw_item)
            quantity = 1
            unit = None
            sku = None
            candidate_count = 0
        if not label:
            continue
        lines.append(
            {
                "line_id": f"line-{len(lines) + 1}",
                "status": "needs_catalog_resolution",
                "source_name": label,
                "quantity": quantity,
                "unit": unit,
                "sku": sku,
                "catalog_item_id": None,
                "catalog_match": None,
                "candidate_count": candidate_count,
                "needs_operator_review": True,
            }
        )

    matched = int(match_summary.get("matched") or sum(1 for line in lines if line["status"] == "catalog_matched"))
    unmatched = int(match_summary.get("unmatched") or sum(1 for line in lines if line["status"] != "catalog_matched"))
    detected = int(match_summary.get("detected") or match_summary.get("total") or len(lines))
    has_contact = bool(contact.get("phone") or contact.get("email"))
    needs_review = bool(match_summary.get("needs_operator_review")) or unmatched > 0 or not has_contact or detected == 0
    is_quote = request_kind == "quote_request"

    if detected == 0:
        recommended_next_step = "separar_items_desde_adjunto"
    elif not has_contact:
        recommended_next_step = "pedir_contacto_y_responder"
    elif unmatched:
        recommended_next_step = "resolver_items_y_cotizar" if is_quote else "resolver_items_y_confirmar"
    else:
        recommended_next_step = "cotizar_y_responder" if is_quote else "confirmar_stock_precio_y_responder"

    return {
        "contract_version": _CRM_ORDER_DRAFT_CONTRACT_VERSION,
        "request_kind": request_kind,
        "request_kind_label": request_kind_label,
        "target_module": "orders",
        "recommended_record": "assisted_order",
        "recommended_next_step": recommended_next_step,
        "needs_operator_review": needs_review,
        "source": {
            "channel": source.get("channel"),
            "input_type": source.get("input_type"),
            "archivo_url": source.get("archivo_url"),
            "archivo_nombre": source.get("archivo_nombre"),
            "text_preview": source.get("text_preview"),
            "attachment_id": source.get("attachment_id"),
            "attachmentInfo": source.get("attachmentInfo"),
            "attachment_info": source.get("attachment_info"),
            "source_attachment": source.get("source_attachment"),
            "sourceAttachment": source.get("sourceAttachment"),
        },
        "contact": contact,
        "contact_state": "available" if has_contact else "missing",
        "lines": lines,
        "catalog_candidate_groups": len(catalog_candidates),
        "summary": {
            "detected": detected,
            "matched": matched,
            "unmatched": unmatched,
            "has_contact": has_contact,
            "needs_operator_review": needs_review,
        },
    }


def _operator_task(
    task_id: str,
    label: str,
    description: str,
    *,
    tone: str = "neutral",
    required: bool = True,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "label": label,
        "description": description,
        "tone": tone,
        "required": required,
    }


def _build_pipeline(
    *,
    text_available: bool,
    detected_count: int,
    matched_count: int,
    unmatched_count: int,
    catalog_matching: bool,
    needs_operator_review: bool,
    customer_has_contact: bool,
) -> list[dict[str, Any]]:
    if not catalog_matching:
        catalog_status = "not_applicable"
    elif detected_count and unmatched_count == 0 and matched_count == detected_count:
        catalog_status = "done"
    else:
        catalog_status = "pending_review"

    return [
        {
            "id": "ocr",
            "label": "Texto extraido",
            "description": "Foto, documento o nota convertida a texto para revisar.",
            "status": "done" if text_available else "pending_review",
        },
        {
            "id": "ai_parse",
            "label": "Lectura automatica",
            "description": "Texto separado en renglones de pedido, cantidades y unidades.",
            "status": "done" if detected_count else "pending_review",
        },
        {
            "id": "catalog_match",
            "label": "Cruce con catalogo",
            "description": "Cruce de productos detectados contra el catalogo del equipo.",
            "status": catalog_status,
        },
        {
            "id": "crm_handoff",
            "label": "Listo para el equipo",
            "description": "Pedido, faltantes y proximo paso quedan listos para revisar.",
            "status": "pending_review" if needs_operator_review else "ready",
        },
        {
            "id": "contact",
            "label": "Contacto",
            "description": "Continuidad por WhatsApp, widget, email, telefono o canal de origen.",
            "status": "done" if customer_has_contact else "pending_review",
        },
    ]


def _build_crm_handoff(
    *,
    channel: str,
    contact: dict[str, Any],
    match_summary: dict[str, Any],
    unmatched_items: list[str],
    needs_operator_review: bool,
) -> dict[str, Any]:
    matched = int(match_summary.get("matched") or 0)
    unmatched = int(match_summary.get("unmatched") or 0)
    detected = int(match_summary.get("detected") or match_summary.get("total") or 0)
    has_contact = bool(contact.get("phone") or contact.get("email"))

    next_steps: list[dict[str, Any]] = []
    if not detected:
        next_steps.append(
            _operator_task(
                "review_ocr_to_items",
                "Separar productos",
                "Abrir texto original, identificar productos principales y pedir aclaracion si la lectura no alcanza.",
                tone="warning",
            )
        )
    if unmatched:
        next_steps.append(
            _operator_task(
                "resolve_catalog_matches",
                "Resolver items sin match",
                "Completar SKU, precio, stock o alternativa para los renglones que no matchearon con catalogo.",
                tone="warning",
            )
        )
    if matched:
        next_steps.append(
            _operator_task(
                "confirm_stock_price",
                "Confirmar stock y precio",
                "Validar disponibilidad, precio vigente y condiciones antes de responder al cliente.",
                tone="success",
            )
        )
    if not has_contact:
        next_steps.append(
            _operator_task(
                "confirm_contact_channel",
                "Confirmar canal de contacto",
                "Responder desde el canal de origen o pedir WhatsApp/email para continuar el pedido.",
                tone="warning",
            )
        )
    next_steps.append(
        _operator_task(
            "send_customer_reply",
            "Responder al cliente",
            "Usar el resumen del pedido para confirmar, corregir o derivar.",
            tone="primary",
        )
    )

    return {
        "label": "Pedido listo para seguimiento",
        "active_channel": channel,
        "channels": ["whatsapp", "chat_widget", "email", "phone"],
        "recommended_next_action": (
            "revisar_y_responder"
            if needs_operator_review
            else "confirmar_stock_precio_y_enviar"
        ),
        "operator_next_steps": next_steps,
        "summary": {
            "detected": detected,
            "matched": matched,
            "unmatched": unmatched,
            "unmatched_items": unmatched_items,
            "needs_operator_review": needs_operator_review,
            "has_contact": has_contact,
        },
    }


def _build_intake_experience(
    *,
    channel: str,
    contact: dict[str, Any],
    catalog_matching: bool,
    needs_operator_review: bool,
    pipeline: list[dict[str, Any]],
    crm_handoff: dict[str, Any],
) -> dict[str, Any]:
    customer_has_contact = bool(contact.get("phone") or contact.get("email"))

    return {
        "contract_version": _INTAKE_EXPERIENCE_CONTRACT_VERSION,
        "render_as": "anonymous_assisted_marketplace_intake",
        "title": "Pedido asistido por foto, papel o texto",
        "summary": (
            "Chatboc interpreta fotos, documentos o notas de pedido y prepara un resumen "
            "para que el equipo responda sin exigir registro previo."
        ),
        "channel": channel,
        "anonymous_intake": True,
        "customer_has_contact": customer_has_contact,
        "catalog_matching": catalog_matching,
        "needs_operator_review": needs_operator_review,
        "input_examples": [
            "Foto de una lista escrita a mano",
            "Pedido pegado desde WhatsApp",
            "PDF, Excel, comprobante o boleta",
        ],
        "capabilities": [
            {
                "id": "handwritten_note_ocr",
                "label": "Notas manuscritas",
                "description": "Acepta fotos de papel, mostrador o listas poco estructuradas.",
                "status": "enabled",
            },
            {
                "id": "catalog_candidate_matching",
                "label": "Candidatos de catalogo",
                "description": "Propone articulos similares cuando no hay match exacto.",
                "status": "enabled" if catalog_matching else "available_for_catalogs",
            },
            {
                "id": "crm_operator_pack",
                "label": "Resumen para el equipo",
                "description": "Incluye resumen, faltantes y proximos pasos.",
                "status": "enabled",
            },
            {
                "id": "omnichannel_reply",
                "label": "Respuesta omnicanal",
                "description": "Continuidad por WhatsApp, widget, email, telefono o canal de origen.",
                "status": "enabled",
            },
        ],
        "pipeline": pipeline,
        "crm_handoff": crm_handoff,
        "frontend_contract": {
            "render_as": "marketplace_assisted_intake",
            "primary_cta": "Subir foto o papel",
            "secondary_cta": "Escribir pedido",
            "show_on_empty_catalog": True,
        },
    }


def _build_assisted_request(
    *,
    channel: str,
    contact: dict[str, Any],
    texto_extraido: str,
    normalized_items: list[dict[str, Any]],
    match_summary: dict[str, Any],
    intake_experience: dict[str, Any],
    crm_handoff: dict[str, Any],
    crm_order_draft: dict[str, Any],
    pyme_id_context: int | None,
) -> dict[str, Any]:
    catalog_matching = bool(pyme_id_context)

    return {
        "contract_version": _ASSISTED_REQUEST_CONTRACT_VERSION,
        "mode": "order_note_upload",
        "request_kind": "order_note",
        "request_kind_label": "nota de pedido",
        "channel": channel,
        "anonymous_intake": True,
        "document_profile": {
            "kind": "order_note",
            "label": "nota de pedido",
            "crm_type": "nota_de_pedido",
            "primary_intent": "create_order",
            "catalog_matching": catalog_matching,
            "input_type": "attachment_preview",
            "input_mode": "file_or_text",
            "supports_anonymous_intake": True,
            "operator_goal": "convertir_a_pedido_o_cotizacion",
        },
        "contact": contact,
        "source": {
            "channel": channel,
            "input_type": "attachment_preview",
            "text_preview": texto_extraido[:500],
        },
        "detected_items": normalized_items,
        "match_summary": match_summary,
        "crm_order_draft": crm_order_draft,
        "intake_experience": intake_experience,
        "crm_handoff": crm_handoff,
    }


def build_order_attachment_preview(
    *,
    texto_extraido: str,
    pyme_id_context: int | None = None,
    telefono: Any = None,
    email: Any = None,
    nombre: Any = None,
    direccion: Any = None,
    channel: str | None = None,
) -> dict[str, Any]:
    raw_text = texto_extraido or ""
    normalized_channel = normalize_sales_channel(channel)
    contact = _build_contact_payload(telefono=telefono, email=email, nombre=nombre, direccion=direccion)
    items_ocr = extraer_lista_pedido_de_texto_con_llm(raw_text, pyme_id_context=pyme_id_context) or []
    normalized_items: list[dict[str, Any]] = []
    matched_items = 0

    for item in items_ocr[:10]:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("nombre_producto_ocr") or item.get("nombre_ocr") or "").strip()
        if not item_name:
            continue
        cantidad = item.get("cantidad_ocr")
        unidad = str(item.get("unidad_ocr") or "").strip() or None
        normalized = {
            "nombre": item_name,
            "cantidad": cantidad if cantidad is not None else 1,
            "unidad": unidad,
            "catalog_match": None,
        }
        if pyme_id_context:
            matched = buscar_item_en_catalogo(
                pyme_user_id=pyme_id_context,
                nombre_busqueda=item_name,
                sku_busqueda=item.get("sku_ocr"),
                umbral_similitud=UMBRAL_SIMILITUD_OCR_PEDIDO,
                es_ocr=True,
            )
            if matched:
                _, precio_float, moneda = parse_precio_flexible(getattr(matched, "precio", None))
                normalized["catalog_match"] = {
                    "catalog_item_id": matched.id,
                    "nombre": matched.nombre,
                    "sku": matched.sku,
                    "precio": precio_float,
                    "moneda": moneda or "ARS",
                    "stock": getattr(matched, "cantidad", None),
                }
                matched_items += 1
        normalized_items.append(normalized)

    unmatched_items = [item["nombre"] for item in normalized_items if not item.get("catalog_match")]
    catalog_matching_enabled = bool(pyme_id_context)
    needs_operator_review = (
        not normalized_items
        or bool(unmatched_items)
        or (catalog_matching_enabled and matched_items == 0)
    )
    match_summary = {
        "matched": matched_items,
        "unmatched": max(len(normalized_items) - matched_items, 0),
        "total": len(normalized_items),
        "detected": len(normalized_items),
        "catalog_matching": catalog_matching_enabled,
        "needs_operator_review": needs_operator_review,
    }
    pipeline = _build_pipeline(
        text_available=bool(_clean_optional_text(raw_text)),
        detected_count=len(normalized_items),
        matched_count=matched_items,
        unmatched_count=match_summary["unmatched"],
        catalog_matching=catalog_matching_enabled,
        needs_operator_review=needs_operator_review,
        customer_has_contact=bool(contact.get("phone") or contact.get("email")),
    )
    crm_handoff = _build_crm_handoff(
        channel=normalized_channel,
        contact=contact,
        match_summary=match_summary,
        unmatched_items=unmatched_items,
        needs_operator_review=needs_operator_review,
    )
    crm_order_draft = build_crm_order_draft(
        request_kind="order_note",
        request_kind_label="nota de pedido",
        source={
            "channel": normalized_channel,
            "input_type": "attachment_preview",
            "text_preview": raw_text[:500],
        },
        contact=contact,
        matched_items=[item for item in normalized_items if item.get("catalog_match")],
        unmatched_items=[item for item in normalized_items if not item.get("catalog_match")],
        catalog_candidates=[],
        match_summary=match_summary,
    )
    crm_handoff["draft_order"] = crm_order_draft
    intake_experience = _build_intake_experience(
        channel=normalized_channel,
        contact=contact,
        catalog_matching=catalog_matching_enabled,
        needs_operator_review=needs_operator_review,
        pipeline=pipeline,
        crm_handoff=crm_handoff,
    )
    assisted_request = _build_assisted_request(
        channel=normalized_channel,
        contact=contact,
        texto_extraido=raw_text,
        normalized_items=normalized_items,
        match_summary=match_summary,
        intake_experience=intake_experience,
        crm_handoff=crm_handoff,
        crm_order_draft=crm_order_draft,
        pyme_id_context=pyme_id_context,
    )

    preview_summary = {
        "items_detalle": [
            {
                "nombre_producto": (item.get("catalog_match") or {}).get("nombre") or item["nombre"],
                "cantidad": item["cantidad"],
            }
            for item in normalized_items
        ]
    }
    order_confirmation = build_order_confirmation_payload(
        cart_summary=preview_summary,
        customer={
            "telefono": telefono,
            "email": email,
            "nombre": nombre,
            "direccion": direccion,
        },
        delivery_address=direccion,
        channel=normalized_channel,
    )

    if normalized_items:
        items_lines = []
        for item in normalized_items[:5]:
            unidad_suffix = f" {item['unidad']}" if item.get("unidad") else ""
            items_lines.append(f"- {item['cantidad']} x {item['nombre']}{unidad_suffix}")
        message = (
            "Procese el adjunto y detecte este pedido preliminar:\n\n"
            + "\n".join(items_lines)
            + "\n\nSi esta bien, puedo seguir armando el pedido o corregimos lo que haga falta."
        )
        options_list = [
            {"texto": "Confirmar pedido", "action_id": "finalizar_pedido_pyme"},
            {"texto": "Corregir pedido", "action_id": "corregir_datos_pedido"},
        ]
    else:
        message = (
            "Procese el adjunto y pude extraer texto, pero todavia no logre separar productos con seguridad.\n\n"
            f"Texto detectado:\n{raw_text[:400]}\n\n"
            "Quieres que lo intentemos interpretar juntos o me indicas los productos principales?"
        )
        options_list = [
            {"texto": "Indicar productos", "action_id": "pyme_hacer_pedido"},
            {"texto": "Hablar con asesor", "action_id": "pyme_hablar_agente"},
        ]

    return {
        "message": message,
        "items_detectados": normalized_items,
        "catalog_match_summary": match_summary,
        "order_confirmation": order_confirmation,
        "confirmation_card": order_confirmation,
        "options_list": options_list,
        "channel": normalized_channel,
        "anonymous_intake": True,
        "catalog_matching": catalog_matching_enabled,
        "needs_operator_review": needs_operator_review,
        "pipeline": pipeline,
        "crm_handoff": crm_handoff,
        "crm_order_draft": crm_order_draft,
        "intake_experience": intake_experience,
        "assisted_request": assisted_request,
    }
