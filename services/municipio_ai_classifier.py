import logging
import os
from typing import Any, Optional

from services.categorias_municipio import CATEGORIAS_RECLAMO, normalizar_texto

logger = logging.getLogger(__name__)


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
