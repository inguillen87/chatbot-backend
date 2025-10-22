"""Utilities to build stable request fingerprints."""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional

from flask import Request


def _coalesce(values: Iterable[Optional[str]]) -> str:
    parts = [value.strip() for value in values if isinstance(value, str) and value.strip()]
    return "|".join(parts)


def hash_fingerprint(request: Request) -> Optional[str]:
    """Return a stable hash derived from request headers and remote address."""

    raw = _coalesce(
        (
            request.headers.get("X-Forwarded-For"),
            request.remote_addr,
            request.headers.get("User-Agent"),
            request.headers.get("Accept-Language"),
        )
    )
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest
