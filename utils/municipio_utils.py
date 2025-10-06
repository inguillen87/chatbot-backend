"""Utilities for working with municipal identifiers."""
from __future__ import annotations

from typing import Any


def get_numeric_municipio_id(value: Any) -> int | None:
    """Return ``value`` as an integer municipality identifier when possible."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            try:
                return int(stripped)
            except ValueError:
                return None
        return None
    return None
