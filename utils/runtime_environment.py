"""Runtime environment classification shared by security-sensitive code."""

from __future__ import annotations

import os
from collections.abc import Mapping


_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
_TEST_ENVIRONMENTS = frozenset({"test", "testing"})
_DEVELOPMENT_ENVIRONMENTS = frozenset({"dev", "development"})
_TRUTHY_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def is_render_runtime(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether Render runtime signals are present.

    ``RENDER_EXTERNAL_URL`` is included because Render injects it for web
    services and older services might not expose ``RENDER`` in their editable
    environment-variable list.
    """

    runtime_env = os.environ if environ is None else environ
    return (
        _normalized(runtime_env.get("RENDER")) in _TRUTHY_VALUES
        or bool(_normalized(runtime_env.get("RENDER_EXTERNAL_URL")))
    )


def is_production_runtime(
    *,
    config_env: object = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Fail closed when any trusted runtime signal says production.

    A stale ``Config.ENV=dev`` must never override Render or ``FLASK_ENV``.
    Conversely, an explicit production value in any supported signal wins over
    a conflicting development value.
    """

    runtime_env = os.environ if environ is None else environ
    if is_render_runtime(runtime_env):
        return True
    signals = tuple(
        normalized
        for normalized in (
            _normalized(config_env),
            _normalized(runtime_env.get("ENV")),
            _normalized(runtime_env.get("FLASK_ENV")),
        )
        if normalized
    )
    if any(value in _PRODUCTION_ENVIRONMENTS for value in signals):
        return True

    # Unknown non-empty environment names are treated as production-like.
    # Development/test behavior must be explicitly selected, not inferred.
    known_non_production = _DEVELOPMENT_ENVIRONMENTS | _TEST_ENVIRONMENTS
    return any(value not in known_non_production for value in signals)


def resolved_runtime_environment(
    *,
    config_env: object = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the canonical legacy environment name used by ``Config``."""

    runtime_env = os.environ if environ is None else environ
    if is_production_runtime(config_env=config_env, environ=runtime_env):
        return "prod"

    for value in (config_env, runtime_env.get("ENV"), runtime_env.get("FLASK_ENV")):
        normalized = _normalized(value)
        if normalized in _TEST_ENVIRONMENTS:
            return "testing"
        if normalized in _DEVELOPMENT_ENVIRONMENTS:
            return "dev"
    return "dev"
