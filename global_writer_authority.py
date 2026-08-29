"""Dependency-free configuration for the shared cutover writer authority.

The database-backed authority is deliberately opt-in.  Keeping the small
configuration parser outside Flask/SQLAlchemy lets process entrypoints decide
whether the guard is enabled without constructing an application or touching
the database.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


GLOBAL_WRITER_AUTHORITY_FLAG = "CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED"
GLOBAL_WRITER_RUNTIME_IDENTITY = "CUTOVER_RUNTIME_IDENTITY"
GLOBAL_WRITER_AUTHORITY_DATABASE_URL = (
    "CUTOVER_GLOBAL_WRITER_AUTHORITY_DATABASE_URL"
)
GLOBAL_WRITER_AUTHORITY_CONTRACT = "cutover.global_writer_authority.v1"
SUPPORTED_WRITER_RUNTIMES = frozenset({"render", "vercel"})

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})


def global_writer_authority_enabled(
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the persistent authority gate was explicitly requested.

    Absence always means disabled.  Once the variable is present, malformed
    values are treated as enabled so a typo cannot silently bypass the guard.
    """

    if config is not None and GLOBAL_WRITER_AUTHORITY_FLAG in config:
        raw_value: Any = config.get(GLOBAL_WRITER_AUTHORITY_FLAG)
    else:
        raw_value = os.getenv(GLOBAL_WRITER_AUTHORITY_FLAG)

    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return False
    normalized = str(raw_value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return True


def configured_writer_runtime(
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a supported declarative runtime identity or ``None``."""

    if config is not None and GLOBAL_WRITER_RUNTIME_IDENTITY in config:
        raw_value: Any = config.get(GLOBAL_WRITER_RUNTIME_IDENTITY)
    else:
        raw_value = os.getenv(GLOBAL_WRITER_RUNTIME_IDENTITY)
    normalized = str(raw_value or "").strip().lower()
    return normalized if normalized in SUPPORTED_WRITER_RUNTIMES else None


def configured_writer_authority_database_url(
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the explicit shared control DSN without falling back to app DB."""

    if config is not None and GLOBAL_WRITER_AUTHORITY_DATABASE_URL in config:
        raw_value: Any = config.get(GLOBAL_WRITER_AUTHORITY_DATABASE_URL)
    else:
        raw_value = os.getenv(GLOBAL_WRITER_AUTHORITY_DATABASE_URL)
    normalized = str(raw_value or "").strip()
    return normalized or None


__all__ = [
    "GLOBAL_WRITER_AUTHORITY_CONTRACT",
    "GLOBAL_WRITER_AUTHORITY_DATABASE_URL",
    "GLOBAL_WRITER_AUTHORITY_FLAG",
    "GLOBAL_WRITER_RUNTIME_IDENTITY",
    "SUPPORTED_WRITER_RUNTIMES",
    "configured_writer_runtime",
    "configured_writer_authority_database_url",
    "global_writer_authority_enabled",
]
