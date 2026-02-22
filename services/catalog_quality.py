import re
from difflib import SequenceMatcher
from statistics import median
from typing import Any


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.-]", "", text).replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def evaluate_catalog_quality(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate catalog rows with confidence score + validation issues.

    The function is side-effect free and returns a new list containing all original
    fields plus: confidence_score, quality_issues and review_required.
    """

    prices = [_to_float(item.get("precio")) for item in items]
    valid_prices = [p for p in prices if p is not None and p > 0]
    median_price = median(valid_prices) if valid_prices else None

    evaluated: list[dict[str, Any]] = []
    known_names: list[str] = []

    for item in items:
        score = 1.0
        issues: list[str] = []

        nombre = str(item.get("nombre") or "").strip()
        categoria = str(item.get("categoria") or "").strip()
        precio = _to_float(item.get("precio"))

        if len(nombre) < 3:
            score -= 0.35
            issues.append("nombre_demasiado_corto")

        if not categoria:
            score -= 0.2
            issues.append("categoria_vacia")

        if precio is None or precio <= 0:
            score -= 0.35
            issues.append("precio_invalido")
        elif median_price and median_price > 0:
            ratio = precio / median_price
            if ratio > 8 or ratio < 0.12:
                score -= 0.25
                issues.append("precio_outlier")

        duplicate = False
        for prev_name in known_names:
            sim = SequenceMatcher(None, nombre.lower(), prev_name.lower()).ratio()
            if sim >= 0.93:
                duplicate = True
                break
        if duplicate:
            score -= 0.2
            issues.append("duplicado_aproximado")

        known_names.append(nombre)

        score = max(0.0, min(1.0, round(score, 2)))
        enriched = dict(item)
        enriched["confidence_score"] = score
        enriched["quality_issues"] = issues
        enriched["review_required"] = score < 0.65 or bool(issues)
        evaluated.append(enriched)

    return evaluated

