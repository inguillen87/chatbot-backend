"""Utilities for working with municipal identifiers."""
from __future__ import annotations

from typing import Any


def _normalize_identifier(value: Any) -> Any | None:
    """Return ``value`` when it looks like a meaningful identifier."""

    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        return trimmed
    if isinstance(value, (int,)):
        return value if value != 0 else None
    return value


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


def resolve_municipio_identifier(owner_user: Any, fallback: Any | None = None) -> Any | None:
    """Infer the municipality identifier linked to ``owner_user``.

    Municipal organisations historically stored their identifier either in
    ``municipio_id`` or – for legacy tenants – implicitly in the user ``id``
    when the ``tipo_chat`` is ``"municipio"``. Employees may instead expose
    the identifier through ``empresa_id``.  This helper consolidates the
    different possibilities so callers can consistently access the most
    specific value available.

    Args:
        owner_user: User-like object associated with the current session.
        fallback: Value returned when no identifier can be determined.

    Returns:
        The detected identifier or ``fallback`` when none can be inferred.
    """

    if owner_user is None:
        return fallback

    municipio_id = _normalize_identifier(getattr(owner_user, "municipio_id", None))
    if municipio_id is not None:
        return municipio_id

    if getattr(owner_user, "tipo_chat", None) == "municipio":
        owner_id = _normalize_identifier(getattr(owner_user, "id", None))
        if owner_id is not None:
            return owner_id

    empresa_id = _normalize_identifier(getattr(owner_user, "empresa_id", None))
    if empresa_id is not None:
        return empresa_id

    return fallback
