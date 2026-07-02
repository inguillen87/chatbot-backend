from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable

from services.categorias_municipio import CATEGORIAS_RECLAMO, normalizar_texto
from services.municipio_ai_classifier import build_reclamo_ai_enrichment, reclamo_ai_thresholds


TICKET_AI_ENRICHMENT_CONTRACT_VERSION = "ticket.ai_enrichment.v1"
TICKET_AI_ADVISORY_POLICY = {
    "advisory_only": True,
    "mutates_operational_state": False,
    "state_mutation_allowed": False,
    "python_handlers_remain_authority": True,
    "requires_operator_confirmation": True,
}

PYME_INTENT_LABELS = {
    "crear_pedido": "crear pedido",
    "consulta_producto": "consulta de producto",
    "consulta_pago": "consulta de pago",
    "consulta_envio": "consulta de envio",
    "soporte_postventa": "soporte postventa",
    "reclamo_cliente": "reclamo de cliente",
    "derivar_humano": "derivar a humano",
}

PYME_HUMAN_ATTENTION_INTENTS = {"derivar_humano", "reclamo_cliente", "soporte_postventa"}

PYME_INTENT_KEYWORDS = {
    "crear_pedido": [
        "comprar",
        "pedido",
        "orden",
        "cotizacion",
        "cotización",
        "presupuesto",
        "carrito",
        "reservar",
        "quiero",
        "necesito",
        "caja",
        "cajas",
        "unidades",
        "pack",
    ],
    "consulta_producto": [
        "stock",
        "precio",
        "catalogo",
        "catálogo",
        "producto",
        "productos",
        "promo",
        "promocion",
        "promoción",
        "descuento",
        "disponible",
        "medida",
        "talle",
        "color",
    ],
    "consulta_pago": [
        "pago",
        "pagar",
        "transferencia",
        "tarjeta",
        "mercado pago",
        "mp",
        "factura",
        "comprobante",
        "cuota",
        "seña",
        "saldo",
    ],
    "consulta_envio": [
        "envio",
        "envío",
        "delivery",
        "domicilio",
        "reparto",
        "retirar",
        "retiro",
        "sucursal",
        "direccion",
        "dirección",
        "zona",
        "flete",
    ],
    "soporte_postventa": [
        "garantia",
        "garantía",
        "devolucion",
        "devolución",
        "cambio",
        "no llego",
        "no llegó",
        "llego mal",
        "llegó mal",
        "postventa",
        "servicio tecnico",
        "servicio técnico",
    ],
    "reclamo_cliente": [
        "reclamo",
        "queja",
        "problema",
        "mal estado",
        "roto",
        "rota",
        "fallado",
        "fallada",
        "no funciona",
        "nadie responde",
        "me cobraron",
    ],
    "derivar_humano": [
        "humano",
        "persona",
        "asesor",
        "vendedor",
        "ventas",
        "atencion",
        "atención",
        "hablar con alguien",
        "llamar",
        "llamada",
        "whatsapp",
    ],
}

PYME_RECOMMENDED_ACTIONS_BY_INTENT = {
    "crear_pedido": [
        {"id": "prepare_order_draft", "label": "Preparar borrador de pedido", "priority": "high"},
        {"id": "confirm_missing_order_data", "label": "Confirmar productos, cantidades y entrega", "priority": "medium"},
    ],
    "consulta_producto": [
        {"id": "send_catalog_or_product_options", "label": "Enviar catalogo o alternativas disponibles", "priority": "medium"},
    ],
    "consulta_pago": [
        {"id": "send_payment_options", "label": "Enviar medios de pago y validar comprobante", "priority": "medium"},
    ],
    "consulta_envio": [
        {"id": "quote_shipping_or_pickup", "label": "Cotizar envio o coordinar retiro", "priority": "medium"},
    ],
    "soporte_postventa": [
        {"id": "open_post_sale_case", "label": "Abrir seguimiento postventa", "priority": "high"},
    ],
    "reclamo_cliente": [
        {"id": "review_customer_complaint", "label": "Revisar reclamo con operador", "priority": "high"},
    ],
    "derivar_humano": [
        {"id": "handoff_to_sales_or_support", "label": "Derivar a una persona del equipo", "priority": "high"},
    ],
}


def _humanize_code(value: Any) -> str:
    return _safe_text(value).replace("_", " ").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        text = _safe_text(value)
        if text:
            return text
    return ""


def _dict_from(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _routing_hint_for_municipio(category: str, crm_hints: dict[str, Any]) -> str:
    normalized = normalizar_texto(category)
    tags = " ".join(_safe_text(tag) for tag in crm_hints.get("tags") or [])
    combined = f"{normalized} {normalizar_texto(tags)}"

    if any(token in combined for token in ("luminaria", "alumbrado", "luz")):
        return "servicios_publicos_luminaria"
    if any(token in combined for token in ("arreglo de calle", "bache", "calle", "riesgo vial", "transito")):
        return "obras_publicas_calles"
    if any(token in combined for token in ("arbolado", "arbol", "espacios verdes")):
        return "espacios_verdes"
    if any(token in combined for token in ("limpieza", "basura", "residuos", "higiene", "salud publica")):
        return "higiene_urbana"
    if any(token in combined for token in ("agua", "perdida", "servicio interrumpido")):
        return "servicios_publicos_agua"
    if "consulta administrativa" in combined:
        return "atencion_ciudadana_tramites"
    return "mesa_operativa_municipal"


def _routing_hint_for_pyme(intent_code: str) -> str:
    return {
        "crear_pedido": "ventas_pedidos",
        "consulta_producto": "ventas_catalogo",
        "consulta_pago": "administracion_pagos",
        "consulta_envio": "logistica_envios",
        "soporte_postventa": "postventa_soporte",
        "reclamo_cliente": "postventa_reclamos",
        "derivar_humano": "atencion_humana",
    }.get(intent_code, "mesa_comercial")


def _add_checklist_item(
    checklist: list[dict[str, Any]],
    seen: set[str],
    item_id: str,
    label: str,
    *,
    priority: str = "medium",
    done: bool = False,
) -> None:
    if item_id in seen:
        return
    seen.add(item_id)
    checklist.append(
        {
            "id": item_id,
            "label": label,
            "priority": priority,
            "done": done,
        }
    )


def _build_municipio_operator_brief(provider_payload: dict[str, Any], crm_hints: dict[str, Any]) -> dict[str, Any]:
    category = _first_text(
        _dict_from(provider_payload.get("category")).get("categoria"),
        _dict_from(provider_payload.get("category")).get("label"),
        crm_hints.get("suggested_queue"),
    )
    priority = _first_text(
        _dict_from(provider_payload.get("priority")).get("prioridad"),
        _dict_from(provider_payload.get("priority")).get("label"),
    )
    sentiment = _first_text(
        _dict_from(provider_payload.get("sentiment")).get("label"),
        _dict_from(provider_payload.get("sentiment")).get("sentiment"),
    )
    risk = _safe_text(crm_hints.get("risk_level")) or "sin_senal"
    risk_key = normalizar_texto(risk)
    priority_risk = risk_key in {"alto", "alta", "critico", "critical", "high", "urgente"}
    requires_human = bool(crm_hints.get("requires_human_attention"))
    requires_location = bool(crm_hints.get("requires_exact_location"))
    requires_photo = bool(crm_hints.get("requires_photo"))

    checklist: list[dict[str, Any]] = []
    seen: set[str] = set()
    _add_checklist_item(
        checklist,
        seen,
        "send_acknowledgement",
        "Responder acuse claro al vecino con proximo paso y canal de seguimiento",
        priority="high" if requires_human else "medium",
    )
    if requires_location:
        _add_checklist_item(
            checklist,
            seen,
            "validate_location",
            "Validar direccion exacta, esquina o referencia antes de derivar cuadrilla",
            priority="high",
        )
    if requires_photo:
        _add_checklist_item(
            checklist,
            seen,
            "request_or_review_photo",
            "Pedir o revisar foto/evidencia para acelerar diagnostico operativo",
            priority="medium",
        )
    if requires_human or priority_risk:
        _add_checklist_item(
            checklist,
            seen,
            "operator_review",
            "Revisar con operador antes de cambiar estado o prometer plazo",
            priority="high",
        )
    _add_checklist_item(
        checklist,
        seen,
        "route_area",
        "Derivar al area responsable y dejar trazabilidad en el historial",
        priority="medium",
    )

    first_reply_parts = [
        "Hola, recibimos tu reclamo y lo vamos a revisar con el area correspondiente.",
    ]
    if requires_location:
        first_reply_parts.append("Para avanzar, confirmame la direccion exacta o una referencia cercana.")
    if requires_photo:
        first_reply_parts.append("Si podes, adjunta una foto para que el equipo tenga mejor contexto.")
    if requires_human or priority_risk:
        first_reply_parts.append("Lo dejamos marcado para revision prioritaria del equipo.")
    first_reply_parts.append("Te avisamos cualquier novedad por este mismo canal.")

    category_copy = _humanize_code(category) or "reclamo municipal"
    priority_copy = _humanize_code(priority) or "sin prioridad IA firme"
    summary = f"Caso de {category_copy} con prioridad {priority_copy}."
    if sentiment:
        summary += f" Tono detectado: {_humanize_code(sentiment)}."

    return {
        "summary": summary,
        "response_tone": "prioritario_empatico" if requires_human or priority_risk else "claro_operativo",
        "routing_hint": _routing_hint_for_municipio(category_copy, crm_hints),
        "recommended_first_reply": " ".join(first_reply_parts),
        "checklist": checklist,
        "confidence_notes": [
            "Guia advisory-only: no cambia estado ni asigna responsables automaticamente.",
            "Confirmar datos sensibles antes de responder plazos o cerrar el caso.",
        ],
    }


def _build_pyme_operator_brief(provider_payload: dict[str, Any], crm_hints: dict[str, Any]) -> dict[str, Any]:
    intent = _dict_from(provider_payload.get("intent"))
    intent_code = _safe_text(intent.get("intent") or crm_hints.get("suggested_queue"))
    intent_label = _first_text(intent.get("label"), PYME_INTENT_LABELS.get(intent_code), intent_code)
    requires_human = bool(crm_hints.get("requires_human_attention"))

    checklist: list[dict[str, Any]] = []
    seen: set[str] = set()
    if intent_code == "crear_pedido":
        _add_checklist_item(checklist, seen, "confirm_items", "Confirmar productos, cantidades y variantes", priority="high")
        _add_checklist_item(checklist, seen, "confirm_delivery", "Definir entrega, retiro o zona de envio", priority="medium")
        _add_checklist_item(checklist, seen, "confirm_payment", "Informar medios de pago y validar comprobante si aplica", priority="medium")
    elif intent_code == "consulta_producto":
        _add_checklist_item(checklist, seen, "send_catalog", "Enviar opciones de catalogo, stock y promociones relevantes", priority="medium")
    elif intent_code == "consulta_pago":
        _add_checklist_item(checklist, seen, "send_payment_options", "Enviar medios de pago, condiciones y datos de facturacion", priority="medium")
    elif intent_code == "consulta_envio":
        _add_checklist_item(checklist, seen, "quote_shipping", "Confirmar direccion o zona para calcular envio/retiro", priority="medium")
    elif intent_code in {"soporte_postventa", "reclamo_cliente"}:
        _add_checklist_item(checklist, seen, "open_post_sale_case", "Abrir seguimiento postventa con evidencia y orden asociada", priority="high")

    if requires_human or intent_code == "derivar_humano":
        _add_checklist_item(checklist, seen, "human_handoff", "Derivar a una persona del equipo y mantener contexto completo", priority="high")
    _add_checklist_item(checklist, seen, "send_first_reply", "Responder con proximo paso concreto para no perder la conversacion", priority="medium")

    replies = {
        "crear_pedido": "Hola, te ayudo a armar el pedido. Confirmame productos, cantidades, entrega y medio de pago para dejarlo listo.",
        "consulta_producto": "Hola, te paso opciones disponibles y promociones. Si me decis cantidad o uso esperado, te recomiendo la mejor alternativa.",
        "consulta_pago": "Hola, te envio los medios de pago disponibles. Si ya tenes comprobante, mandalo por este canal y lo validamos.",
        "consulta_envio": "Hola, coordinamos envio o retiro. Confirmame direccion o zona y te digo disponibilidad y costo.",
        "soporte_postventa": "Hola, revisamos tu caso postventa. Mandame numero de pedido y una foto si aplica para acelerar la solucion.",
        "reclamo_cliente": "Hola, recibimos tu reclamo y lo revisa una persona del equipo. Te vamos a pedir los datos necesarios para resolverlo.",
        "derivar_humano": "Hola, te derivo con una persona del equipo manteniendo el contexto de esta conversacion.",
    }

    return {
        "summary": f"Consulta pyme detectada como {_humanize_code(intent_label) or 'atencion comercial'}.",
        "response_tone": "comercial_consultivo" if not requires_human else "empatico_resolutivo",
        "routing_hint": _routing_hint_for_pyme(intent_code),
        "recommended_first_reply": replies.get(intent_code, "Hola, recibimos tu consulta. Te respondemos con el proximo paso para avanzar sin perder contexto."),
        "checklist": checklist,
        "confidence_notes": [
            "Guia advisory-only: no crea pedidos ni modifica estados sin confirmacion del operador.",
            "Usar catalogo, stock y condiciones reales antes de confirmar precio o entrega.",
        ],
    }


def _build_operator_brief(scope: str, provider_payload: dict[str, Any], crm_hints: dict[str, Any]) -> dict[str, Any] | None:
    if scope == "municipio":
        return _build_municipio_operator_brief(provider_payload, crm_hints)
    if scope == "pyme":
        return _build_pyme_operator_brief(provider_payload, crm_hints)
    return None


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bounded_float_env(name: str, default: float, *, min_value: float = 0.0, max_value: float = 1.0) -> float:
    value = _float_env(name, default)
    return max(min_value, min(max_value, value))


def ticket_ai_thresholds() -> dict[str, Any]:
    return {
        "municipio": reclamo_ai_thresholds(),
        "pyme": {
            "intent_min_score": _bounded_float_env("HUGGINGFACE_PYME_INTENT_MIN_SCORE", 0.62),
        },
    }


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _contains_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = normalizar_texto(phrase)
    if not normalized_phrase:
        return False
    return f" {normalized_phrase} " in f" {normalized_text} "


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    normalized = normalizar_texto(text)
    hits: list[str] = []
    for keyword in keywords:
        if _contains_phrase(normalized, keyword):
            hits.append(keyword)
    return hits


def _score_from_hits(hits: list[str], *, base: float = 0.64, per_hit: float = 0.07) -> float:
    if not hits:
        return 0.0
    long_phrase_bonus = min(0.08, len([hit for hit in hits if len(normalizar_texto(hit).split()) >= 2]) * 0.02)
    return round(min(0.94, base + (len(hits) * per_hit) + long_phrase_bonus), 4)


def _read_field(source: Any, *keys: str) -> Any:
    if isinstance(source, dict):
        for key in keys:
            if key in source:
                return source.get(key)
        return None
    for key in keys:
        value = getattr(source, key, None)
        if value not in (None, ""):
            return value
    return None


def _ticket_text(ticket: Any, comments: Iterable[Any] | None = None, *, max_comment_chars: int = 4000) -> str:
    extra = getattr(ticket, "datos_extra", None)
    if not isinstance(extra, dict):
        extra = {}
    contact = extra.get("contact") if isinstance(extra.get("contact"), dict) else {}

    parts = [
        _safe_text(_read_field(ticket, "asunto")),
        _safe_text(extra.get("title")),
        _safe_text(_read_field(ticket, "categoria")),
        _safe_text(extra.get("type")),
        _safe_text(_read_field(ticket, "pregunta")),
        _safe_text(_read_field(ticket, "detalles")),
        _safe_text(_read_field(ticket, "descripcion")),
        _safe_text(extra.get("description")),
        _safe_text(_read_field(ticket, "direccion")),
        _safe_text(extra.get("address")),
        _safe_text(_read_field(ticket, "distrito")),
        _safe_text(contact.get("name")),
        _safe_text(contact.get("phone")),
    ]

    remaining = max_comment_chars
    for comment in comments or []:
        text = _safe_text(_read_field(comment, "comentario", "body", "texto", "message"))
        if not text:
            continue
        if remaining <= 0:
            break
        parts.append(text[:remaining])
        remaining -= len(text)

    return "\n".join(part for part in parts if part).strip()


def _ranked_code_candidates(results: list[dict], labels_by_code: dict[str, str], limit: int = 6) -> list[dict]:
    normalized_code_by_label = {
        normalizar_texto(label): code for code, label in labels_by_code.items()
    }
    ranked: list[dict] = []
    seen: set[str] = set()
    for item in results or []:
        code = normalized_code_by_label.get(normalizar_texto(str(item.get("label") or "")))
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        ranked.append({"code": code, "label": labels_by_code[code], "score": round(score, 4)})
        if len(ranked) >= limit:
            break
    return ranked


def _local_pyme_intent_fallback(text: str, *, fallback_reason: str = "huggingface_unavailable") -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for code, keywords in PYME_INTENT_KEYWORDS.items():
        hits = _keyword_hits(text, keywords)
        score = _score_from_hits(hits)
        if score <= 0:
            continue
        candidates.append(
            {
                "code": code,
                "label": PYME_INTENT_LABELS[code],
                "score": score,
                "matched_keywords": hits[:8],
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    min_score = ticket_ai_thresholds()["pyme"]["intent_min_score"]
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None

    return {
        "provider": "deterministic_local_fallback",
        "fallback_reason": fallback_reason,
        "intent": best["code"],
        "label": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "matched_keywords": best.get("matched_keywords", []),
        "candidates": candidates[:6],
    }


def _build_pyme_enrichment(text: str) -> dict[str, Any]:
    intent = None
    fallback_reason = "huggingface_unavailable"
    try:
        from services.huggingface_inference_service import classify_zero_shot

        results = classify_zero_shot(text, list(PYME_INTENT_LABELS.values()), multi_label=False)
    except Exception:
        results = None
    else:
        if results:
            fallback_reason = "huggingface_below_threshold"

    candidates = _ranked_code_candidates(results or [], PYME_INTENT_LABELS)
    if candidates:
        min_score = ticket_ai_thresholds()["pyme"]["intent_min_score"]
        best = candidates[0]
        if float(best["score"]) >= min_score:
            intent = {
                "provider": "huggingface_zero_shot",
                "intent": best["code"],
                "label": best["label"],
                "score": best["score"],
                "threshold": min_score,
                "meets_threshold": True,
                "candidates": candidates,
            }
        else:
            intent = _local_pyme_intent_fallback(text, fallback_reason=fallback_reason)
    else:
        if results:
            fallback_reason = "huggingface_no_valid_candidates"
        intent = _local_pyme_intent_fallback(text, fallback_reason=fallback_reason)

    requires_human = bool(intent and intent.get("intent") in PYME_HUMAN_ATTENTION_INTENTS)
    recommended_actions = list(PYME_RECOMMENDED_ACTIONS_BY_INTENT.get(intent.get("intent"), [])) if intent else []
    tags = []
    if intent:
        tags.append(f"intent:{intent['intent']}")
    if requires_human:
        tags.append("requires_human_attention")

    crm_hints = {
        "suggested_queue": intent.get("intent") if intent else None,
        "requires_human_attention": requires_human,
        "tags": tags,
        "recommended_actions": recommended_actions,
        "advisory_only": True,
        "mutates_operational_state": False,
    }
    provider_payload = {
        "contract_version": "pyme.ticket_ai_enrichment.v1",
        "input_chars": len(text),
        "provider_family": "huggingface",
        "advisory_policy": dict(TICKET_AI_ADVISORY_POLICY),
        "thresholds": ticket_ai_thresholds()["pyme"],
        "intent": intent,
        "crm_hints": crm_hints,
        "state_mutation": {
            "requested": False,
            "applied": False,
            "reason": "ai_enrichment_is_advisory_only",
        },
    }
    provider_payload["operator_brief"] = _build_pyme_operator_brief(provider_payload, crm_hints)

    return {
        **provider_payload,
    }


def _municipio_categories(tenant: Any | None = None) -> list[str]:
    categories = list(CATEGORIAS_RECLAMO)
    tenant_categories = getattr(tenant, "categorias", None)
    try:
        for category in tenant_categories or []:
            name = _safe_text(getattr(category, "nombre", None))
            if name and name not in categories:
                categories.append(name)
    except Exception:
        pass
    return categories


def build_ticket_ai_enrichment(
    ticket: Any,
    *,
    scope: str,
    comments: Iterable[Any] | None = None,
    tenant: Any | None = None,
) -> dict[str, Any]:
    """Build an on-demand AI enrichment contract for admin CRM views."""

    normalized_scope = (scope or "municipio").strip().lower()
    comments_list = list(comments or [])
    text = _ticket_text(ticket, comments_list)
    tenant_id = getattr(ticket, "tenant_id", None) or getattr(tenant, "id", None)
    current_state = getattr(ticket, "estado", None)
    thresholds = ticket_ai_thresholds()

    if normalized_scope == "municipio":
        provider_payload = build_reclamo_ai_enrichment(text, _municipio_categories(tenant))
        crm_hints = provider_payload.get("crm_hints") or {}
    elif normalized_scope == "pyme":
        provider_payload = _build_pyme_enrichment(text)
        crm_hints = provider_payload.get("crm_hints") or {}
    else:
        provider_payload = {
            "reason": "unsupported_scope",
            "provider_family": "huggingface",
            "advisory_policy": dict(TICKET_AI_ADVISORY_POLICY),
        }
        crm_hints = {}
    operator_brief = (
        provider_payload.get("operator_brief")
        if isinstance(provider_payload.get("operator_brief"), dict)
        else _build_operator_brief(normalized_scope, provider_payload, crm_hints)
    )

    return {
        "contract_version": TICKET_AI_ENRICHMENT_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticket_id": getattr(ticket, "id", None),
        "ticket_type": normalized_scope,
        "tenant_id": tenant_id,
        "advisory_policy": dict(TICKET_AI_ADVISORY_POLICY),
        "thresholds": thresholds.get(normalized_scope, {}),
        "source": {
            "text_chars": len(text),
            "comments_count": len(comments_list),
            "has_location": bool(getattr(ticket, "direccion", None) or getattr(ticket, "latitud", None)),
            "has_media": bool(getattr(ticket, "foto_url_directa", None)),
            "operational_state_before": current_state,
        },
        "huggingface": provider_payload,
        "crm_hints": crm_hints,
        "operator_brief": operator_brief,
        "state_mutation": {
            "requested": False,
            "applied": False,
            "estado_before": current_state,
            "estado_after": current_state,
            "reason": "ai_enrichment_is_advisory_only",
        },
        "persisted": False,
        "secret_values_exposed": False,
    }
