from __future__ import annotations

import os
from collections import Counter
from typing import Any, Mapping, Sequence

from services.huggingface_inference_service import (
    classify_zero_shot,
    huggingface_configured,
    zero_shot_enabled,
)


AI_INSIGHTS_CONTRACT_VERSION = "huggingface.ai_insights.v1"
MAP_AI_LAYERS_CONTRACT_VERSION = "huggingface.map_ai_layers.v1"
WHATSAPP_AI_RUNTIME_CONTRACT_VERSION = "huggingface.whatsapp_ai_runtime.v1"
AI_INSIGHTS_ADVISORY_POLICY = {
    "advisory_only": True,
    "mutates_operational_state": False,
    "state_mutation_allowed": False,
    "python_handlers_remain_authority": True,
    "requires_operator_confirmation": True,
}
DEFAULT_AI_INSIGHTS_THRESHOLDS = {
    "intent_min_score": 0.56,
    "risk_min_score": 0.56,
    "sentiment_min_score": 0.52,
    "map_risk_min_weight": 1.4,
}

INTENT_LABELS = [
    "reclamo de servicio publico",
    "tramite o turno",
    "encuesta o votacion",
    "pedido o venta",
    "soporte humano",
    "consulta general",
    "pago o cobro",
]
RISK_LABELS = [
    "normal",
    "prioridad alta",
    "critico",
    "necesita ubicacion exacta",
    "necesita foto o evidencia",
    "derivar a agente",
]
SENTIMENT_LABELS = [
    "positivo",
    "neutral",
    "negativo",
    "frustracion o enojo",
]

DEFAULT_LABEL_GROUPS = {
    "intent": INTENT_LABELS,
    "risk": RISK_LABELS,
    "sentiment": SENTIMENT_LABELS,
}

LABEL_CODES = {
    "reclamo de servicio publico": "public_service_claim",
    "tramite o turno": "procedure_or_appointment",
    "encuesta o votacion": "survey_or_vote",
    "pedido o venta": "order_or_sales",
    "soporte humano": "human_support",
    "consulta general": "general_query",
    "pago o cobro": "payment_or_collection",
    "normal": "normal",
    "prioridad alta": "high_priority",
    "critico": "critical",
    "necesita ubicacion exacta": "needs_exact_location",
    "necesita foto o evidencia": "needs_photo_or_evidence",
    "derivar a agente": "handoff_to_agent",
    "positivo": "positive",
    "neutral": "neutral",
    "negativo": "negative",
    "frustracion o enojo": "frustration_or_anger",
}

KEYWORDS = {
    "reclamo de servicio publico": (
        "reclamo",
        "bache",
        "luminaria",
        "arbol",
        "basura",
        "perdida de agua",
        "limpieza",
        "semaforo",
        "calle",
        "roto",
    ),
    "tramite o turno": (
        "tramite",
        "turno",
        "licencia",
        "dni",
        "partida",
        "habilitacion",
        "expediente",
    ),
    "encuesta o votacion": ("encuesta", "votar", "votacion", "sondeo", "opinion", "urna"),
    "pedido o venta": ("pedido", "comprar", "stock", "precio", "carrito", "producto", "envio", "catalogo"),
    "soporte humano": ("humano", "agente", "persona", "operador", "hablar con alguien", "chat en vivo"),
    "consulta general": ("consulta", "informacion", "horario", "direccion", "ayuda", "como hago"),
    "pago o cobro": ("pagar", "cuota", "comprobante", "factura", "deuda", "tasa", "impuesto"),
    "critico": (
        "urgente",
        "peligro",
        "incendio",
        "gas",
        "accidente",
        "cable",
        "arbol caido",
        "semaforo apagado",
    ),
    "prioridad alta": ("no funciona", "roto", "sin luz", "perdida", "fuga", "inundacion", "demora"),
    "necesita ubicacion exacta": ("ubicacion", "direccion", "esquina", "calle", "altura", "donde"),
    "necesita foto o evidencia": ("foto", "imagen", "adjunto", "evidencia", "comprobante", "archivo"),
    "derivar a agente": ("humano", "agente", "persona", "operador", "no me responde"),
    "positivo": ("gracias", "excelente", "bien", "conforme", "solucionado"),
    "negativo": ("mal", "problema", "reclamo", "queja", "roto", "demora", "no funciona"),
    "frustracion o enojo": ("enojo", "harto", "desastre", "verguenza", "horrible", "otra vez", "nadie"),
}

DEFAULT_LABEL = {
    "intent": "consulta general",
    "risk": "normal",
    "sentiment": "neutral",
}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bounded_float_env(name: str, default: float, *, min_value: float = 0.0, max_value: float = 1.0) -> float:
    value = _float_env(name, default)
    return max(min_value, min(max_value, value))


def ai_insights_thresholds() -> dict[str, float]:
    return {
        "intent_min_score": _bounded_float_env(
            "HUGGINGFACE_AI_INTENT_MIN_SCORE",
            DEFAULT_AI_INSIGHTS_THRESHOLDS["intent_min_score"],
        ),
        "risk_min_score": _bounded_float_env(
            "HUGGINGFACE_AI_RISK_MIN_SCORE",
            DEFAULT_AI_INSIGHTS_THRESHOLDS["risk_min_score"],
        ),
        "sentiment_min_score": _bounded_float_env(
            "HUGGINGFACE_AI_SENTIMENT_MIN_SCORE",
            DEFAULT_AI_INSIGHTS_THRESHOLDS["sentiment_min_score"],
        ),
        "map_risk_min_weight": max(
            0.1,
            min(_float_env("HUGGINGFACE_MAP_RISK_MIN_WEIGHT", DEFAULT_AI_INSIGHTS_THRESHOLDS["map_risk_min_weight"]), 20.0),
        ),
    }


def _threshold_for_group(group: str) -> float:
    thresholds = ai_insights_thresholds()
    if group == "risk":
        return thresholds["risk_min_score"]
    if group == "sentiment":
        return thresholds["sentiment_min_score"]
    return thresholds["intent_min_score"]


def _clean_text(value: Any, *, max_chars: int = 4000) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())[:max_chars]


def _code(label: str) -> str:
    return LABEL_CODES.get(label, label.strip().lower().replace(" ", "_"))


def _score_label(text: str, label: str, group: str) -> float:
    if not text:
        return 0.25 if label == DEFAULT_LABEL.get(group) else 0.05
    normalized = text.lower()
    hits = sum(1 for keyword in KEYWORDS.get(label, ()) if keyword in normalized)
    if hits:
        return min(0.94, 0.56 + hits * 0.12)
    if label == DEFAULT_LABEL.get(group):
        return 0.42
    return 0.18


def _local_candidates(text: str, labels: Sequence[str], group: str) -> list[dict[str, Any]]:
    candidates = [
        {
            "label": label,
            "code": _code(label),
            "score": round(_score_label(text, label, group), 4),
            "provider": "deterministic_local_fallback",
        }
        for label in labels
    ]
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return candidates


def _hf_candidates(text: str, labels: Sequence[str], *, multi_label: bool) -> list[dict[str, Any]] | None:
    try:
        result = classify_zero_shot(text, list(labels), multi_label=multi_label)
    except Exception:
        return None
    if not result:
        return None
    candidates: list[dict[str, Any]] = []
    for item in result:
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        candidates.append(
            {
                "label": label,
                "code": _code(label),
                "score": round(score, 4),
                "provider": "huggingface_zero_shot",
            }
        )
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return candidates or None


def _group_result(text: str, group: str, labels: Sequence[str]) -> dict[str, Any]:
    hf_result = _hf_candidates(text, labels, multi_label=(group != "sentiment"))
    candidates = hf_result or _local_candidates(text, labels, group)
    top = candidates[0] if candidates else {}
    threshold = _threshold_for_group(group)
    top_score = float(top.get("score") or 0)
    return {
        "group": group,
        "top_label": top.get("label"),
        "top_code": top.get("code"),
        "score": top.get("score", 0),
        "threshold": threshold,
        "meets_threshold": top_score >= threshold,
        "provider": top.get("provider") or "deterministic_local_fallback",
        "candidates": candidates[:6],
    }


def _effective_group_value(group_result: Mapping[str, Any], group: str) -> tuple[str, str]:
    default_label = DEFAULT_LABEL.get(group, "")
    default_code = _code(default_label) if default_label else ""
    top_label = str(group_result.get("top_label") or default_label)
    top_code = str(group_result.get("top_code") or default_code)
    if bool(group_result.get("meets_threshold")) or top_label == default_label:
        return top_code, top_label
    return default_code, default_label


def build_text_ai_insights(
    text: Any,
    *,
    domain: str = "operations",
    label_groups: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    cleaned = _clean_text(text)
    groups = {
        name: _group_result(cleaned, name, labels)
        for name, labels in (label_groups or DEFAULT_LABEL_GROUPS).items()
        if labels
    }
    used_hf = any((group.get("provider") == "huggingface_zero_shot") for group in groups.values())
    intent = groups.get("intent") or {}
    risk = groups.get("risk") or {}
    sentiment = groups.get("sentiment") or {}
    intent_code, intent_label = _effective_group_value(intent, "intent")
    risk_code, _risk_label = _effective_group_value(risk, "risk")
    sentiment_code, _sentiment_label = _effective_group_value(sentiment, "sentiment")
    return {
        "contract_version": AI_INSIGHTS_CONTRACT_VERSION,
        "provider_family": "huggingface",
        "mode": "huggingface_zero_shot" if used_hf else "deterministic_local_fallback",
        "domain": domain,
        "advisory_policy": dict(AI_INSIGHTS_ADVISORY_POLICY),
        "thresholds": ai_insights_thresholds(),
        "hf_status": {
            "configured": huggingface_configured(),
            "zero_shot_enabled": zero_shot_enabled(),
            "used": used_hf,
            "fallback_reason": None if used_hf else "zero_shot_disabled_or_unavailable",
        },
        "text_length": len(cleaned),
        "groups": groups,
        "summary": {
            "dominant_intent": intent_code or "general_query",
            "dominant_intent_label": intent_label or "consulta general",
            "risk_signal": risk_code or "normal",
            "sentiment": sentiment_code or "neutral",
            "requires_human_attention": (risk_code in {"critical", "handoff_to_agent"})
            or (sentiment_code == "frustration_or_anger"),
            "requires_location_focus": risk_code == "needs_exact_location",
            "advisory_only": True,
            "mutates_operational_state": False,
            "secret_values_exposed": False,
        },
    }


def _item_text(item: Mapping[str, Any]) -> str:
    parts = [
        item.get("text"),
        item.get("label"),
        item.get("title"),
        item.get("category"),
        item.get("status"),
        item.get("channel"),
        item.get("source"),
        item.get("address"),
    ]
    return _clean_text(" ".join(str(part) for part in parts if part))


def _risk_level(insights: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> str:
    summary = insights.get("summary") if isinstance(insights, Mapping) else {}
    risk = (summary or {}).get("risk_signal")
    if risk == "critical":
        return "critical"
    if risk in {"high_priority", "handoff_to_agent"}:
        return "high"
    if any(str(item.get("status") or "").lower() in {"vencido", "overdue", "breached"} for item in items):
        return "high"
    return "normal"


def _recommended_actions(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if summary.get("requires_human_attention"):
        actions.append(
            {
                "id": "open_human_review_queue",
                "label": "Revisar conversaciones con riesgo",
                "priority": "high",
                "ui_hint": "open_ai_attention_queue",
            }
        )
    if summary.get("requires_location_focus"):
        actions.append(
            {
                "id": "request_or_validate_location",
                "label": "Validar ubicaciones exactas",
                "priority": "medium",
                "ui_hint": "open_geocoding_queue",
            }
        )
    if summary.get("dominant_intent") == "survey_or_vote":
        actions.append(
            {
                "id": "inspect_survey_participation",
                "label": "Analizar participacion territorial",
                "priority": "medium",
                "ui_hint": "open_survey_heatmap",
            }
        )
    if not actions:
        actions.append(
            {
                "id": "keep_monitoring_ai_signals",
                "label": "Mantener monitoreo IA",
                "priority": "low",
                "ui_hint": "open_ai_summary",
            }
        )
    return actions


def build_collection_ai_insights(
    items: Sequence[Mapping[str, Any]] | None,
    *,
    domain: str = "operations",
    max_items: int = 40,
) -> dict[str, Any]:
    normalized_items = [item for item in (items or []) if isinstance(item, Mapping)]
    text_items = [_item_text(item) for item in normalized_items[:max_items]]
    text = "\n".join(item for item in text_items if item)
    insights = build_text_ai_insights(text, domain=domain)
    summary = dict(insights.get("summary") or {})
    summary["risk_level"] = _risk_level(insights, normalized_items)
    category_counter = Counter(str(item.get("category") or "unknown").lower() for item in normalized_items)
    channel_counter = Counter(str(item.get("channel") or "unknown").lower() for item in normalized_items)
    source_counter = Counter(str(item.get("source") or "unknown").lower() for item in normalized_items)
    summary["map_layer_hints"] = [
        "ai_risk_pulses",
        "whatsapp_activity",
        "survey_participation",
        "category_heat",
    ]
    return {
        **insights,
        "advisory_policy": dict(AI_INSIGHTS_ADVISORY_POLICY),
        "collection": {
            "items_analyzed": len(normalized_items),
            "text_items_analyzed": len([item for item in text_items if item]),
            "categories": [{"key": key, "count": int(value)} for key, value in category_counter.most_common(8)],
            "channels": [{"key": key, "count": int(value)} for key, value in channel_counter.most_common(8)],
            "sources": [{"key": key, "count": int(value)} for key, value in source_counter.most_common(8)],
        },
        "summary": summary,
        "recommended_actions": _recommended_actions(summary),
        "frontend_contract": {
            "recommended_widgets": ["ai_summary_cards", "risk_queue", "intent_breakdown", "map_layer_toggles"],
            "refresh_seconds": 30,
            "safe_to_render_without_hf_token": True,
            "advisory_only": True,
        },
    }


def _point_lat_lng(point: Mapping[str, Any]) -> tuple[float | None, float | None]:
    lat = point.get("lat")
    lng = point.get("lng", point.get("lon"))
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None


def _compact_point(point: Mapping[str, Any], *, reason: str) -> dict[str, Any] | None:
    lat, lng = _point_lat_lng(point)
    if lat is None or lng is None:
        return None
    return {
        "id": point.get("id"),
        "lat": lat,
        "lng": lng,
        "weight": round(float(point.get("weight") or point.get("w") or 1.0), 4),
        "category": point.get("category") or point.get("categoria"),
        "channel": point.get("channel") or point.get("canal"),
        "source": point.get("source"),
        "status": point.get("status"),
        "reason_code": reason,
    }


def build_map_ai_layers(
    points: Sequence[Mapping[str, Any]] | None,
    *,
    category_layers: Any = None,
    insights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_points = [point for point in (points or []) if isinstance(point, Mapping)]
    thresholds = ai_insights_thresholds()
    risk_weight_threshold = thresholds["map_risk_min_weight"]
    risk_points: list[dict[str, Any]] = []
    whatsapp_points: list[dict[str, Any]] = []
    survey_points: list[dict[str, Any]] = []
    for point in normalized_points:
        status = str(point.get("status") or "").lower()
        channel = str(point.get("channel") or point.get("canal") or "").lower()
        source = str(point.get("source") or "").lower()
        weight = float(point.get("weight") or point.get("w") or 1.0)
        if weight >= risk_weight_threshold or status in {"vencido", "overdue", "breached"}:
            compact = _compact_point(point, reason="high_weight_or_overdue")
            if compact:
                risk_points.append(compact)
        if "whatsapp" in channel:
            compact = _compact_point(point, reason="whatsapp_activity")
            if compact:
                whatsapp_points.append(compact)
        if source == "survey" or "survey" in source or "encuesta" in source:
            compact = _compact_point(point, reason="survey_participation")
            if compact:
                survey_points.append(compact)

    return {
        "contract_version": MAP_AI_LAYERS_CONTRACT_VERSION,
        "provider_family": "huggingface",
        "advisory_policy": dict(AI_INSIGHTS_ADVISORY_POLICY),
        "thresholds": {"map_risk_min_weight": risk_weight_threshold},
        "visual_preset": "premium_city_intelligence_map",
        "preferred_visualization": "interactive_globe_heatmap",
        "supports_globe": True,
        "style_tokens": {
            "risk": "#EF4444",
            "whatsapp": "#10B981",
            "survey": "#7C3AED",
            "attention": "#F59E0B",
            "route": "#06B6D4",
            "neutral": "#2563EB",
        },
        "layers": {
            "risk_pulses": {"type": "animated_scatter", "points": risk_points[:250], "count": len(risk_points)},
            "whatsapp_activity": {"type": "pulse_points", "points": whatsapp_points[:250], "count": len(whatsapp_points)},
            "survey_participation": {"type": "territory_heat", "points": survey_points[:250], "count": len(survey_points)},
            "category_heat": {"type": "category_heatmap", "source": "category_layers", "enabled": bool(category_layers)},
        },
        "animation": {
            "enabled": True,
            "modes": ["pulse_hotspots", "soft_heat_sweep", "globe_arc_focus"],
            "reduced_motion_behavior": "static_layers_same_data",
        },
        "interaction_model": {
            "hover": ["label", "category", "channel", "ai_reason"],
            "click": ["open_record", "filter_by_cell", "inspect_ai_signals"],
            "toggles": ["risk_pulses", "whatsapp_activity", "survey_participation", "category_heat"],
        },
        "insight_summary": (insights or {}).get("summary") if isinstance(insights, Mapping) else {},
        "frontend_contract": {
            "map_engines": ["maplibre", "deckgl", "google"],
            "preferred_camera": "city_or_region_globe",
            "fallback": "2d_heatmap_with_same_layers",
            "advisory_only": True,
        },
    }


def build_whatsapp_ai_runtime_contract() -> dict[str, Any]:
    return {
        "contract_version": WHATSAPP_AI_RUNTIME_CONTRACT_VERSION,
        "provider_family": "huggingface",
        "configured": huggingface_configured(),
        "zero_shot_enabled": zero_shot_enabled(),
        "classification_groups": {
            "intent": INTENT_LABELS,
            "risk": RISK_LABELS,
            "sentiment": SENTIMENT_LABELS,
        },
        "use_cases": [
            "priorizar reclamos por riesgo",
            "detectar pedidos, ventas y pagos desde WhatsApp",
            "segmentar encuestas y votaciones",
            "marcar conversaciones para agente humano",
            "alimentar mapas de calor y analitica operativa",
        ],
        "runtime_policy": {
            "must_not_expose_tokens": True,
            "python_validates_actions": True,
            "advisory_only": True,
            "mutates_operational_state": False,
            "safe_fallback_without_hf": "deterministic_local_fallback",
        },
        "frontend_contract": {
            "badges": ["hf_ready", "ai_risk_scoring", "survey_signal_scoring"],
            "recommended_panels": ["ai_intent_breakdown", "attention_queue", "map_ai_layers"],
        },
    }
