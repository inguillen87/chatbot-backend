"""Privacy helpers to safely share analytics data."""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional


def pseudoanonymize(value: Optional[object], *, salt: str = "civic-analytics") -> Optional[str]:
    """Return a deterministic pseudonym for ``value``.

    The helper hashes the provided ``value`` with a configurable ``salt`` so
    that exported analytics never expose personally identifiable information
    such as DNI numbers or raw phone numbers.  Only the first 16 hex
    characters of the digest are returned to keep identifiers compact while
    preserving an astronomically low collision probability for the expected
    datasets.
    """

    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    digest = hashlib.sha256(f"{salt}|{text}".encode("utf-8")).hexdigest()
    return digest[:16]


def privacy_preserving_total(values: Iterable[int], *, min_count: int = 3) -> int:
    """Aggregate a sequence of integers dropping small groups.

    Metrics derived from fewer than ``min_count`` records are removed to reduce
    the risk of re-identification.  The helper returns ``0`` when the input
    does not reach the threshold so calling code can still render charts
    without special-casing ``None`` values.
    """

    total = 0
    count = 0
    for value in values:
        total += int(value)
        count += 1

    if count < min_count:
        return 0
    return total
