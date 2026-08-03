"""Fail-closed network policy for external providers during tests.

Production behavior is unchanged.  Test suites must opt in independently for
each provider so enabling a fake OpenAI transport cannot accidentally enable a
    real Gemini, Cohere, Ollama, geocoding, or Twilio fallback.
"""

from __future__ import annotations

import os
from typing import Any


_PROVIDER_TEST_OPT_INS = {
    "openai": "OPENAI_ALLOW_NETWORK_IN_TESTS",
    "gemini": "GEMINI_ALLOW_NETWORK_IN_TESTS",
    "cohere": "COHERE_ALLOW_NETWORK_IN_TESTS",
    "ollama": "OLLAMA_ALLOW_NETWORK_IN_TESTS",
    "geocoding": "GEOCODING_ALLOW_NETWORK_IN_TESTS",
    "twilio": "TWILIO_ALLOW_NETWORK_IN_TESTS",
}
_TRUTHY = {"1", "true", "yes", "on"}


class LLMProviderNetworkDisabledError(RuntimeError):
    """Stable pre-request error raised when a test provider is offline."""

    safe_to_fallback = True

    def __init__(self, provider: str):
        normalized = _normalize_provider(provider)
        self.provider = normalized
        self.code = f"{normalized}_test_network_disabled"
        super().__init__(self.code)


def _flag_enabled(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _normalize_provider(provider: object) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized not in _PROVIDER_TEST_OPT_INS:
        raise ValueError("unsupported_llm_provider")
    return normalized


def _config_flag(config: Any, name: str) -> bool:
    if config is None or not hasattr(config, "get"):
        return False
    return _flag_enabled(config.get(name))


def _testing_and_opt_in(app: Any, opt_in_name: str) -> tuple[bool, bool]:
    testing = _flag_enabled(os.getenv("TESTING"))
    opted_in = _flag_enabled(os.getenv(opt_in_name))

    app_config = getattr(app, "config", None)
    testing = testing or _config_flag(app_config, "TESTING")
    opted_in = opted_in or _config_flag(app_config, opt_in_name)

    try:
        from flask import current_app, has_app_context

        if has_app_context():
            testing = testing or _config_flag(current_app.config, "TESTING")
            opted_in = opted_in or _config_flag(current_app.config, opt_in_name)
    except RuntimeError:  # pragma: no cover - defensive Flask boundary
        pass

    return testing, opted_in


def llm_provider_network_allowed(provider: object, app: Any = None) -> bool:
    """Return whether the named provider may perform network I/O.

    Outside testing all providers retain their production behavior.  Under
    testing, only the provider-specific opt-in is accepted; there is no global
    switch that can silently enable fallback providers.
    """

    normalized = _normalize_provider(provider)
    testing, opted_in = _testing_and_opt_in(
        app,
        _PROVIDER_TEST_OPT_INS[normalized],
    )
    return not testing or opted_in


def require_llm_provider_network(provider: object, app: Any = None) -> None:
    """Raise a redacted, retry-safe error before any forbidden provider I/O."""

    normalized = _normalize_provider(provider)
    if not llm_provider_network_allowed(normalized, app):
        raise LLMProviderNetworkDisabledError(normalized)


# Generic names make the policy reusable outside the LLM stack while keeping
# backwards compatibility with the already-integrated bridge modules.
ProviderNetworkDisabledError = LLMProviderNetworkDisabledError


def provider_network_allowed(provider: object, app: Any = None) -> bool:
    return llm_provider_network_allowed(provider, app)


def require_provider_network(provider: object, app: Any = None) -> None:
    require_llm_provider_network(provider, app)


__all__ = [
    "LLMProviderNetworkDisabledError",
    "ProviderNetworkDisabledError",
    "llm_provider_network_allowed",
    "provider_network_allowed",
    "require_llm_provider_network",
    "require_provider_network",
]
