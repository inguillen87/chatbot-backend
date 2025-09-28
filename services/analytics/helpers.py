"""Shared helpers for analytics computations."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from statistics import median
from typing import Iterable, List, Optional, Sequence, Tuple


def tenant_as_int(tenant_id: str) -> Optional[int]:
    try:
        return int(tenant_id)
    except (TypeError, ValueError):
        return None


def to_minutes(delta) -> Optional[float]:
    if not delta:
        return None
    seconds = delta.total_seconds()
    if math.isnan(seconds):
        return None
    return round(seconds / 60.0, 2)


def compute_percentiles(values: Sequence[float]) -> dict:
    if not values:
        return {"p50": None, "p90": None, "p95": None}
    ordered = sorted(values)
    return {
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "p95": _percentile(ordered, 0.95),
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 2)
    k = (len(values) - 1) * percentile
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(values[int(k)], 2)
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return round(d0 + d1, 2)


def rolling_average(points: Sequence[Tuple[datetime, float]], window: int = 7) -> List[Tuple[datetime, float]]:
    if not points:
        return []
    results: List[Tuple[datetime, float]] = []
    acc = 0.0
    queue: List[float] = []
    for index, (ts, value) in enumerate(points):
        queue.append(value)
        acc += value
        if len(queue) > window:
            acc -= queue.pop(0)
        divisor = min(window, len(queue))
        results.append((ts, round(acc / divisor, 2)))
    return results


def compute_ctr(sent: int, responded: int) -> float:
    if not sent:
        return 0.0
    return round(responded / sent * 100.0, 2)


def safe_ratio(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100.0, 2)


def mode(values: Iterable) -> Optional:
    counter = Counter(values)
    if not counter:
        return None
    return counter.most_common(1)[0][0]
