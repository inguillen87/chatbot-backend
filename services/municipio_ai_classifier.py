import logging
import os
from typing import Any, Optional

from services.categorias_municipio import CATEGORIAS_RECLAMO, CATEGORIAS_SINONIMOS, normalizar_texto

logger = logging.getLogger(__name__)

RECLAMO_AI_ENRICHMENT_CONTRACT_VERSION = "municipio.reclamo_ai_enrichment.v1"
RECLAMO_AI_ADVISORY_POLICY = {
    "advisory_only": True,
    "mutates_operational_state": False,
    "state_mutation_allowed": False,
    "python_handlers_remain_authority": True,
    "requires_operator_confirmation": True,
}
DEFAULT_RECLAMO_AI_THRESHOLDS = {
    "category_min_score": 0.72,
    "priority_min_score": 0.66,
    "signal_min_score": 0.62,
    "sentiment_min_score": 0.56,
}
DEFAULT_RECLAMO_AI_MAX_TEXT_CHARS = 4000

RECLAMO_OPERATIONAL_SIGNAL_LABELS = {
    "riesgo_personas": "riesgo para personas",
    "riesgo_vial": "riesgo vial o transito",
    "servicio_interrumpido": "servicio publico interrumpido",
    "salud_publica": "salud publica o higiene",
    "inspeccion_urgente": "requiere inspeccion urgente",
    "requiere_evidencia": "requiere foto o evidencia",
    "requiere_ubicacion_exacta": "requiere ubicacion exacta",
    "consulta_administrativa": "consulta administrativa",
}

RECLAMO_SENTIMENT_LABELS = {
    "frustracion_alta": "frustrado o enojado",
    "preocupacion": "preocupado",
    "neutral": "neutral",
    "positivo": "satisfecho",
}

LOCAL_PRIORITY_SIGNALS = {
    "urgente": [
        "urgente",
        "emergencia",
        "peligro",
        "peligroso",
        "se esta cayendo",
        "por caer",
        "cable suelto",
        "incendio",
        "fuego",
        "accidente",
        "lastimado",
        "herido",
        "electrocut",
    ],
    "alta": [
        "sin luz",
        "sin agua",
        "semaforo apagado",
        "esquina oscura",
        "calle cortada",
        "perdida de agua",
        "basural",
        "ratas",
        "mosquitos",
        "humo",
    ],
}

LOCAL_OPERATIONAL_SIGNAL_KEYWORDS = {
    "riesgo_personas": [
        "peligro",
        "peligroso",
        "por caer",
        "se cae",
        "cable suelto",
        "electrocut",
        "herido",
        "lastimado",
        "ninos",
        "escuela",
    ],
    "riesgo_vial": [
        "transito",
        "vehiculo",
        "autos",
        "motos",
        "calle",
        "esquina",
        "ruta",
        "semaforo",
        "choque",
        "accidente",
    ],
    "servicio_interrumpido": [
        "sin luz",
        "sin agua",
        "corte",
        "apagado",
        "fuera de servicio",
        "roto",
        "rota",
        "no funciona",
    ],
    "salud_publica": [
        "basura",
        "residuos",
        "ratas",
        "mosquitos",
        "olor",
        "cloaca",
        "aguas servidas",
        "insectos",
        "plaga",
        "pastizal",
    ],
    "inspeccion_urgente": [
        "urgente",
        "hoy",
        "inmediato",
        "peligro",
        "incendio",
        "fuego",
        "humo",
        "por caer",
        "se esta cayendo",
    ],
    "requiere_evidencia": [
        "foto",
        "imagen",
        "video",
        "adjunto",
        "mando foto",
        "se ve",
    ],
    "requiere_ubicacion_exacta": [
        "direccion",
        "altura",
        "esquina",
        "interseccion",
        "frente a",
        "barrio",
        "manzana",
        "lote",
        "ubicacion",
    ],
    "consulta_administrativa": [
        "tramite",
        "expediente",
        "habilitacion",
        "permiso",
        "consulta",
        "certificado",
        "boleta",
    ],
}

LOCAL_SENTIMENT_KEYWORDS = {
    "frustracion_alta": [
        "cansado",
        "harto",
        "enojo",
        "enojado",
        "nadie responde",
        "otra vez",
        "reclame",
        "verguenza",
    ],
    "preocupacion": [
        "preocupado",
        "miedo",
        "peligro",
        "peligroso",
        "urgente",
        "ninos",
        "escuela",
        "accidente",
    ],
    "positivo": [
        "gracias",
        "excelente",
        "buenisimo",
        "solucionado",
        "conforme",
    ],
}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bounded_float_env(name: str, default: float, *, min_value: float = 0.0, max_value: float = 1.0) -> float:
    value = _float_env(name, default)
    return max(min_value, min(max_value, value))


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def reclamo_ai_thresholds() -> dict[str, float]:
    return {
        "category_min_score": _bounded_float_env(
            "HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE",
            DEFAULT_RECLAMO_AI_THRESHOLDS["category_min_score"],
        ),
        "priority_min_score": _bounded_float_env(
            "HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE",
            DEFAULT_RECLAMO_AI_THRESHOLDS["priority_min_score"],
        ),
        "signal_min_score": _bounded_float_env(
            "HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE",
            DEFAULT_RECLAMO_AI_THRESHOLDS["signal_min_score"],
        ),
        "sentiment_min_score": _bounded_float_env(
            "HUGGINGFACE_SENTIMENT_MIN_SCORE",
            DEFAULT_RECLAMO_AI_THRESHOLDS["sentiment_min_score"],
        ),
    }


def _clean_reclamo_text(text: Any) -> str:
    max_chars = max(500, min(_int_env("AI_RECLAMO_ENRICHMENT_MAX_TEXT_CHARS", DEFAULT_RECLAMO_AI_MAX_TEXT_CHARS), 12000))
    return str(text or "").strip()[:max_chars]


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


def _score_from_hits(hits: list[str], *, base: float = 0.66, per_hit: float = 0.08) -> float:
    if not hits:
        return 0.0
    long_phrase_bonus = min(0.08, len([hit for hit in hits if len(normalizar_texto(hit).split()) >= 2]) * 0.02)
    return round(min(0.94, base + (len(hits) * per_hit) + long_phrase_bonus), 4)


def _normalize_result_label(label: Any, allowed_labels: list[str]) -> Optional[str]:
    normalized = normalizar_texto(str(label or ""))
    if not normalized:
        return None
    for candidate in allowed_labels:
        if normalizar_texto(candidate) == normalized:
            return candidate
    return None


def _top_candidates(results: list[dict], allowed_labels: list[str], limit: int = 3) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()
    for item in results or []:
        label = _normalize_result_label(item.get("label"), allowed_labels)
        if not label or label in seen:
            continue
        seen.add(label)
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        candidates.append({"label": label, "score": round(score, 4)})
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return candidates[:limit]


def _local_category_candidates(text: str, allowed_labels: list[str], limit: int = 3) -> list[dict]:
    normalized_allowed = {normalizar_texto(label): label for label in allowed_labels}
    synonym_map = {
        normalizar_texto(category): keywords
        for category, keywords in CATEGORIAS_SINONIMOS.items()
        if normalizar_texto(category) in normalized_allowed
    }
    candidates: list[dict] = []
    for normalized_category, keywords in synonym_map.items():
        hits = _keyword_hits(text, keywords + [normalized_category])
        score = _score_from_hits(hits, base=0.68, per_hit=0.07)
        if score <= 0:
            continue
        candidates.append(
            {
                "label": normalized_allowed[normalized_category],
                "score": score,
                "matched_keywords": hits[:6],
            }
        )
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return candidates[:limit]


def _local_category_fallback(text: str, allowed_labels: list[str]) -> Optional[dict]:
    candidates = _local_category_candidates(text, allowed_labels)
    if not candidates:
        return None
    min_score = reclamo_ai_thresholds()["category_min_score"]
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None
    return {
        "categoria": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "provider": "deterministic_local_fallback",
        "fallback_reason": "huggingface_unavailable",
        "matched_keywords": best.get("matched_keywords", []),
        "candidates": candidates,
    }


def _local_priority_fallback(text: str) -> Optional[dict]:
    candidates: list[dict] = []
    for label, keywords in LOCAL_PRIORITY_SIGNALS.items():
        hits = _keyword_hits(text, keywords)
        score = _score_from_hits(hits, base=0.69, per_hit=0.08)
        if score > 0:
            candidates.append({"label": label, "score": score, "matched_keywords": hits[:6]})
    if not candidates:
        candidates.append({"label": "normal", "score": 0.67, "matched_keywords": []})
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    min_score = reclamo_ai_thresholds()["priority_min_score"]
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None
    return {
        "prioridad": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "provider": "deterministic_local_fallback",
        "fallback_reason": "huggingface_unavailable",
        "matched_keywords": best.get("matched_keywords", []),
        "candidates": [{"label": item["label"], "score": item["score"]} for item in candidates[:3]],
    }


def _local_operational_signals_fallback(text: str) -> Optional[dict]:
    candidates: list[dict] = []
    for code, keywords in LOCAL_OPERATIONAL_SIGNAL_KEYWORDS.items():
        hits = _keyword_hits(text, keywords)
        score = _score_from_hits(hits, base=0.64, per_hit=0.08)
        if score <= 0:
            continue
        candidates.append(
            {
                "code": code,
                "label": RECLAMO_OPERATIONAL_SIGNAL_LABELS[code],
                "score": score,
                "matched_keywords": hits[:6],
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    min_score = reclamo_ai_thresholds()["signal_min_score"]
    selected = [candidate for candidate in candidates if float(candidate["score"]) >= min_score]
    if not selected:
        return None
    signal_codes = {candidate["code"] for candidate in selected}
    top_score = float(selected[0].get("score") or 0)
    return {
        "provider": "deterministic_local_fallback",
        "fallback_reason": "huggingface_unavailable",
        "threshold": min_score,
        "meets_threshold": True,
        "risk_level": _risk_level_from_signal_codes(signal_codes, top_score),
        "requires_photo": "requiere_evidencia" in signal_codes,
        "requires_exact_location": "requiere_ubicacion_exacta" in signal_codes,
        "requires_human_attention": bool(
            {"riesgo_personas", "riesgo_vial", "inspeccion_urgente"} & signal_codes
        ),
        "signals": selected,
        "candidates": candidates[:8],
    }


def _local_sentiment_fallback(text: str) -> Optional[dict]:
    candidates: list[dict] = []
    for code, keywords in LOCAL_SENTIMENT_KEYWORDS.items():
        hits = _keyword_hits(text, keywords)
        score = _score_from_hits(hits, base=0.64, per_hit=0.08)
        if score <= 0:
            continue
        candidates.append(
            {
                "code": code,
                "label": RECLAMO_SENTIMENT_LABELS[code],
                "score": score,
                "matched_keywords": hits[:5],
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    min_score = reclamo_ai_thresholds()["sentiment_min_score"]
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None
    return {
        "provider": "deterministic_local_fallback",
        "fallback_reason": "huggingface_unavailable",
        "sentiment": best["code"],
        "label": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "matched_keywords": best.get("matched_keywords", []),
        "candidates": candidates[:4],
    }


def _top_code_candidates(results: list[dict], labels_by_code: dict[str, str], limit: int = 8) -> list[dict]:
    normalized_code_by_label = {
        normalizar_texto(label): code for code, label in labels_by_code.items()
    }
    candidates: list[dict] = []
    seen: set[str] = set()
    for item in results or []:
        normalized_label = normalizar_texto(str(item.get("label") or ""))
        code = normalized_code_by_label.get(normalized_label)
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        candidates.append(
            {
                "code": code,
                "label": labels_by_code[code],
                "score": round(score, 4),
            }
        )
    candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return candidates[:limit]


def infer_reclamo_category(text: str, categories: list[str] | None = None) -> Optional[dict]:
    """Return a Hugging Face category suggestion for municipal complaints.

    This is intentionally advisory. It only returns a category when the
    zero-shot score clears a configurable threshold, so the existing heuristics
    and LLM extraction remain the primary behavior.
    """

    if not text or len(str(text).strip()) < 8:
        return None

    allowed_labels = [label for label in (categories or CATEGORIAS_RECLAMO) if label]
    if not allowed_labels:
        return None

    try:
        from services.huggingface_inference_service import classify_zero_shot

        results = classify_zero_shot(str(text), allowed_labels, multi_label=False)
    except Exception as exc:
        logger.warning("Hugging Face reclamo category inference skipped: %s", exc)
        return _local_category_fallback(str(text), allowed_labels)

    if not results:
        return _local_category_fallback(str(text), allowed_labels)

    min_score = reclamo_ai_thresholds()["category_min_score"]
    candidates = _top_candidates(results, allowed_labels)
    if not candidates:
        return None

    best = candidates[0]
    if float(best["score"]) < min_score:
        logger.info(
            "Hugging Face category below threshold: %s < %s",
            best["score"],
            min_score,
        )
        return None

    return {
        "categoria": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "provider": "huggingface_zero_shot",
        "candidates": candidates,
    }


def infer_reclamo_priority(text: str) -> Optional[dict]:
    """Infer an advisory priority signal for future CRM/SLA automation."""

    if not text or len(str(text).strip()) < 8:
        return None

    labels = ["normal", "alta", "urgente"]
    try:
        from services.huggingface_inference_service import classify_zero_shot

        results = classify_zero_shot(
            str(text),
            labels,
            multi_label=False,
        )
    except Exception as exc:
        logger.warning("Hugging Face reclamo priority inference skipped: %s", exc)
        return _local_priority_fallback(str(text))

    candidates = _top_candidates(results or [], labels)
    if not candidates:
        return _local_priority_fallback(str(text))

    min_score = reclamo_ai_thresholds()["priority_min_score"]
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None

    return {
        "prioridad": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "provider": "huggingface_zero_shot",
        "candidates": candidates,
    }


def _risk_level_from_signal_codes(signal_codes: set[str], top_score: float) -> str:
    if {"riesgo_personas", "inspeccion_urgente"} & signal_codes and top_score >= 0.74:
        return "critico"
    if {"riesgo_personas", "riesgo_vial", "inspeccion_urgente"} & signal_codes:
        return "alto"
    if {"servicio_interrumpido", "salud_publica"} & signal_codes:
        return "medio"
    if signal_codes:
        return "bajo"
    return "sin_senal"


def infer_reclamo_operational_signals(text: str) -> Optional[dict]:
    """Return advisory operational signals for routing and CRM triage.

    This layer deliberately stays advisory: it does not mutate ticket state and
    it only emits compact flags that the bot/admin UI can use to ask better
    follow-up questions or highlight risky cases.
    """

    if not text or len(str(text).strip()) < 8:
        return None

    labels = list(RECLAMO_OPERATIONAL_SIGNAL_LABELS.values())
    try:
        from services.huggingface_inference_service import classify_zero_shot

        results = classify_zero_shot(str(text), labels, multi_label=True)
    except Exception as exc:
        logger.warning("Hugging Face reclamo signal inference skipped: %s", exc)
        return _local_operational_signals_fallback(str(text))

    candidates = _top_code_candidates(results or [], RECLAMO_OPERATIONAL_SIGNAL_LABELS)
    if not candidates:
        return _local_operational_signals_fallback(str(text))

    min_score = reclamo_ai_thresholds()["signal_min_score"]
    selected = [candidate for candidate in candidates if float(candidate["score"]) >= min_score]
    if not selected:
        return None

    signal_codes = {candidate["code"] for candidate in selected}
    top_score = float(selected[0].get("score") or 0)
    return {
        "provider": "huggingface_zero_shot",
        "threshold": min_score,
        "meets_threshold": True,
        "risk_level": _risk_level_from_signal_codes(signal_codes, top_score),
        "requires_photo": "requiere_evidencia" in signal_codes,
        "requires_exact_location": "requiere_ubicacion_exacta" in signal_codes,
        "requires_human_attention": bool(
            {"riesgo_personas", "riesgo_vial", "inspeccion_urgente"} & signal_codes
        ),
        "signals": selected,
        "candidates": candidates,
    }


def infer_reclamo_sentiment(text: str) -> Optional[dict]:
    """Infer citizen sentiment without replacing the main LLM response."""

    if not text or len(str(text).strip()) < 8:
        return None

    labels = list(RECLAMO_SENTIMENT_LABELS.values())
    try:
        from services.huggingface_inference_service import classify_zero_shot

        results = classify_zero_shot(str(text), labels, multi_label=False)
    except Exception as exc:
        logger.warning("Hugging Face reclamo sentiment inference skipped: %s", exc)
        return _local_sentiment_fallback(str(text))

    candidates = _top_code_candidates(results or [], RECLAMO_SENTIMENT_LABELS, limit=4)
    if not candidates:
        return _local_sentiment_fallback(str(text))

    min_score = reclamo_ai_thresholds()["sentiment_min_score"]
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None

    return {
        "provider": "huggingface_zero_shot",
        "sentiment": best["code"],
        "label": best["label"],
        "score": best["score"],
        "threshold": min_score,
        "meets_threshold": True,
        "candidates": candidates,
    }


def build_reclamo_ai_enrichment(text: str, categories: list[str] | None = None) -> dict:
    """Build a compact Hugging Face enrichment payload for municipal claims."""

    raw_text = str(text or "").strip()
    cleaned_text = _clean_reclamo_text(raw_text)
    thresholds = reclamo_ai_thresholds()
    category = infer_reclamo_category(cleaned_text, categories)
    priority = infer_reclamo_priority(cleaned_text)
    signals = infer_reclamo_operational_signals(cleaned_text)
    sentiment = infer_reclamo_sentiment(cleaned_text)

    tags: list[str] = []
    if category and category.get("categoria"):
        tags.append(str(category["categoria"]))
    if priority and priority.get("prioridad"):
        tags.append(f"prioridad:{priority['prioridad']}")
    if signals:
        tags.extend(f"signal:{item['code']}" for item in signals.get("signals", []) if item.get("code"))
    if sentiment and sentiment.get("sentiment"):
        tags.append(f"sentiment:{sentiment['sentiment']}")

    deduped_tags = []
    seen: set[str] = set()
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        deduped_tags.append(tag)

    requires_human_attention = bool((signals or {}).get("requires_human_attention")) or bool(
        (sentiment or {}).get("sentiment") == "frustracion_alta"
    )
    recommended_actions: list[dict[str, Any]] = []
    if requires_human_attention:
        recommended_actions.append(
            {
                "id": "review_reclamo_ai_signal",
                "label": "Revisar reclamo con operador",
                "priority": "high",
            }
        )
    if (signals or {}).get("requires_exact_location"):
        recommended_actions.append(
            {
                "id": "validate_exact_location",
                "label": "Validar ubicacion exacta",
                "priority": "medium",
            }
        )
    if (signals or {}).get("requires_photo"):
        recommended_actions.append(
            {
                "id": "request_or_review_photo",
                "label": "Solicitar o revisar evidencia",
                "priority": "medium",
            }
        )

    return {
        "contract_version": RECLAMO_AI_ENRICHMENT_CONTRACT_VERSION,
        "input_chars": len(cleaned_text),
        "provider_family": "huggingface",
        "advisory_policy": dict(RECLAMO_AI_ADVISORY_POLICY),
        "thresholds": thresholds,
        "source": {
            "raw_input_chars": len(raw_text),
            "analyzed_chars": len(cleaned_text),
            "truncated": len(cleaned_text) < len(raw_text),
        },
        "category": category,
        "priority": priority,
        "operational_signals": signals,
        "sentiment": sentiment,
        "crm_hints": {
            "risk_level": (signals or {}).get("risk_level") or "sin_senal",
            "requires_photo": bool((signals or {}).get("requires_photo")),
            "requires_exact_location": bool((signals or {}).get("requires_exact_location")),
            "requires_human_attention": requires_human_attention,
            "tags": deduped_tags,
            "recommended_actions": recommended_actions,
            "advisory_only": True,
            "mutates_operational_state": False,
        },
        "state_mutation": {
            "requested": False,
            "applied": False,
            "reason": "ai_enrichment_is_advisory_only",
        },
    }
