"""Shared, dependency-free writer fence for controlled database cutovers.

The web process, standalone workers, Render cron commands and Vercel cron
routes all read the same disabled-by-default flag.  This module intentionally
does not import Flask, the ORM or provider SDKs so process entrypoints can stop
before application bootstrap and external I/O.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Callable, TypeVar


CUTOVER_WRITER_FENCE_FLAG = "CUTOVER_WRITER_FENCE_ENABLED"
BACKGROUND_FENCE_CONTRACT_VERSION = "cutover.background_writer_fence.v1"
_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})
_CUTOVER_WRITER_VIEW_ATTRIBUTE = "__cutover_writer_view__"
_CUTOVER_READ_ONLY_VIEW_ATTRIBUTE = "__cutover_read_only_view__"
_ViewCallable = TypeVar("_ViewCallable", bound=Callable[..., Any])


def cutover_writer_fence_enabled(
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the shared writer fence is explicitly enabled."""

    raw_value: Any
    if config is not None and CUTOVER_WRITER_FENCE_FLAG in config:
        raw_value = config.get(CUTOVER_WRITER_FENCE_FLAG)
    else:
        raw_value = os.getenv(CUTOVER_WRITER_FENCE_FLAG)

    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return False

    normalized = str(raw_value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False

    # This flag protects the final source-of-truth snapshot.  A present but
    # misspelled value must freeze writers instead of silently reopening them.
    return True


def cutover_writer_view(view: _ViewCallable) -> _ViewCallable:
    """Mark a GET/HEAD view that can mutate state or invoke a provider.

    Flask resolves the endpoint before ``before_request`` hooks run, so the
    HTTP fence can reject these views before authentication, ORM access, audit
    writes or provider polling.  Truly read-only GET/HEAD views remain usable.
    """

    setattr(view, _CUTOVER_WRITER_VIEW_ATTRIBUTE, True)
    return view


def is_cutover_writer_view(view: Any) -> bool:
    """Return whether a resolved Flask view was explicitly marked as a writer."""

    return bool(getattr(view, _CUTOVER_WRITER_VIEW_ATTRIBUTE, False))


def cutover_read_only_view(view: _ViewCallable) -> _ViewCallable:
    """Mark an unsafe-method view whose handler is proven read-only.

    This exception is explicit and narrow. Unmarked POST/PUT/PATCH/DELETE
    views remain fenced before authentication or request processing.
    """

    setattr(view, _CUTOVER_READ_ONLY_VIEW_ATTRIBUTE, True)
    return view


def is_cutover_read_only_view(view: Any) -> bool:
    """Return whether an unsafe-method view is explicitly read-only."""

    return bool(getattr(view, _CUTOVER_READ_ONLY_VIEW_ATTRIBUTE, False))


def background_writer_fence_report(component: str) -> dict[str, Any]:
    """Build a payload-free, stable report for a fenced background writer."""

    return {
        "contract_version": BACKGROUND_FENCE_CONTRACT_VERSION,
        "component": str(component),
        "executed": False,
        "reason_code": "cutover_writer_fence_enabled",
        "status": "fenced",
    }


def log_background_writer_fence(logger: Any, report: Mapping[str, Any]) -> None:
    """Emit a payload-free startup attestation for one fenced process."""

    logger.warning(
        "cutover_writer_fence_active contract_version=%s component=%s "
        "status=%s executed=%s",
        report.get("contract_version"),
        report.get("component"),
        report.get("status"),
        report.get("executed"),
    )


__all__ = [
    "BACKGROUND_FENCE_CONTRACT_VERSION",
    "CUTOVER_WRITER_FENCE_FLAG",
    "background_writer_fence_report",
    "cutover_writer_fence_enabled",
    "cutover_read_only_view",
    "cutover_writer_view",
    "is_cutover_read_only_view",
    "is_cutover_writer_view",
    "log_background_writer_fence",
]
