"""Central OpenAI model defaults for application-owned workloads.

Keep model selection role-aware: Sol is the quality-first default, Terra is the
balanced/latency-sensitive default, and speech keeps its own dedicated model.
Environment overrides are resolved at call time so Flask/dotenv initialization
and tests can change configuration without reloading modules.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


DEFAULT_OPENAI_SOL_MODEL = "gpt-5.6-sol"
DEFAULT_OPENAI_TERRA_MODEL = "gpt-5.6-terra"
DEFAULT_OPENAI_TTS_MODEL = "gpt-4o-mini-tts"


def resolve_openai_model(
    env_name: str,
    default: str,
    *,
    environ: Mapping[str, Any] | None = None,
) -> str:
    """Return a non-empty environment override or the supplied role default."""

    source = os.environ if environ is None else environ
    configured = str(source.get(env_name) or "").strip()
    return configured or default


def chat_completion_compatibility_options(
    model: str,
    *,
    legacy_temperature: float = 0.0,
) -> dict[str, Any]:
    """Preserve non-reasoning behavior across legacy and GPT-5.6 chat calls.

    GPT-5.6 does not accept the legacy sampling knobs used by these deterministic
    extractors. Its explicit ``none`` effort preserves the effective behavior of
    the prior GPT-4/mini calls without relying on the new medium default.
    """

    normalized = str(model or "").strip().lower()
    if normalized == "gpt-5.6" or normalized.startswith("gpt-5.6-"):
        return {"reasoning_effort": "none"}
    return {"temperature": legacy_temperature}
