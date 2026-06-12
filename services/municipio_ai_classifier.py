import logging
import os
from typing import Any, Optional

from services.categorias_municipio import CATEGORIAS_RECLAMO, normalizar_texto

logger = logging.getLogger(__name__)

RECLAMO_AI_ENRICHMENT_CONTRACT_VERSION = "municipio.reclamo_ai_enrichment.v1"

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


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


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
        if len(candidates) >= limit:
            break
    return candidates


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
        if len(candidates) >= limit:
            break
    return candidates


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
        return None

    if not results:
        return None

    min_score = _float_env("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", 0.72)
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
        return None

    candidates = _top_candidates(results or [], labels)
    if not candidates:
        return None

    min_score = _float_env("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", 0.66)
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None

    return {
        "prioridad": best["label"],
        "score": best["score"],
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
        return None

    candidates = _top_code_candidates(results or [], RECLAMO_OPERATIONAL_SIGNAL_LABELS)
    if not candidates:
        return None

    min_score = _float_env("HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE", 0.62)
    selected = [candidate for candidate in candidates if float(candidate["score"]) >= min_score]
    if not selected:
        return None

    signal_codes = {candidate["code"] for candidate in selected}
    top_score = float(selected[0].get("score") or 0)
    return {
        "provider": "huggingface_zero_shot",
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
        return None

    candidates = _top_code_candidates(results or [], RECLAMO_SENTIMENT_LABELS, limit=4)
    if not candidates:
        return None

    min_score = _float_env("HUGGINGFACE_SENTIMENT_MIN_SCORE", 0.56)
    best = candidates[0]
    if float(best["score"]) < min_score:
        return None

    return {
        "provider": "huggingface_zero_shot",
        "sentiment": best["code"],
        "label": best["label"],
        "score": best["score"],
        "candidates": candidates,
    }


def build_reclamo_ai_enrichment(text: str, categories: list[str] | None = None) -> dict:
    """Build a compact Hugging Face enrichment payload for municipal claims."""

    cleaned_text = str(text or "").strip()
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

    return {
        "contract_version": RECLAMO_AI_ENRICHMENT_CONTRACT_VERSION,
        "input_chars": len(cleaned_text),
        "provider_family": "huggingface",
        "category": category,
        "priority": priority,
        "operational_signals": signals,
        "sentiment": sentiment,
        "crm_hints": {
            "risk_level": (signals or {}).get("risk_level") or "sin_senal",
            "requires_photo": bool((signals or {}).get("requires_photo")),
            "requires_exact_location": bool((signals or {}).get("requires_exact_location")),
            "requires_human_attention": bool((signals or {}).get("requires_human_attention"))
            or bool((sentiment or {}).get("sentiment") == "frustracion_alta"),
            "tags": deduped_tags,
        },
    }
