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


def _ticket_text(ticket: Any, comments: Iterable[Any] | None = None, *, max_comment_chars: int = 4000) -> str:
    parts = [
        _safe_text(getattr(ticket, "asunto", None)),
        _safe_text(getattr(ticket, "categoria", None)),
        _safe_text(getattr(ticket, "pregunta", None)),
        _safe_text(getattr(ticket, "detalles", None)),
        _safe_text(getattr(ticket, "direccion", None)),
        _safe_text(getattr(ticket, "distrito", None)),
    ]

    remaining = max_comment_chars
    for comment in comments or []:
        text = _safe_text(getattr(comment, "comentario", None))
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


def _build_pyme_enrichment(text: str) -> dict[str, Any]:
    intent = None
    try:
        from services.huggingface_inference_service import classify_zero_shot

        results = classify_zero_shot(text, list(PYME_INTENT_LABELS.values()), multi_label=False)
    except Exception:
        results = None

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

    requires_human = bool(intent and intent.get("intent") in {"derivar_humano", "reclamo_cliente", "soporte_postventa"})
    tags = []
    if intent:
        tags.append(f"intent:{intent['intent']}")
    if requires_human:
        tags.append("requires_human_attention")

    return {
        "contract_version": "pyme.ticket_ai_enrichment.v1",
        "input_chars": len(text),
        "provider_family": "huggingface",
        "advisory_policy": dict(TICKET_AI_ADVISORY_POLICY),
        "thresholds": ticket_ai_thresholds()["pyme"],
        "intent": intent,
        "crm_hints": {
            "suggested_queue": intent.get("intent") if intent else None,
            "requires_human_attention": requires_human,
            "tags": tags,
            "advisory_only": True,
            "mutates_operational_state": False,
        },
        "state_mutation": {
            "requested": False,
            "applied": False,
            "reason": "ai_enrichment_is_advisory_only",
        },
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
