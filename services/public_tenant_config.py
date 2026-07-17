from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


_PRIVATE_CONFIG_SECTIONS = frozenset(
    {
        "credentials",
        "integration_credentials",
        "oauth_credentials",
        "provider_credentials",
        "secret_store",
        "secrets",
        "twilio_tech_provider",
    }
)

_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "authorization",
        "authorization_header",
        "access_token",
        "account_sid",
        "api_key",
        "api_secret",
        "app_secret",
        "auth_token",
        "bearer_token",
        "client_secret",
        "credentials_ref",
        "entity_token",
        "password",
        "password_hash",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "signing_secret",
        "token",
        "twilio_auth_token",
        "verification_token",
        "verify_token",
        "webhook_secret",
        "widget_token",
        "widget_tokens",
    }
)

_SENSITIVE_CONFIG_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_api_secret",
    "_auth_token",
    "_bearer_token",
    "_client_secret",
    "_credentials",
    "_credentials_ref",
    "_encryption_key",
    "_password",
    "_password_hash",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_secret_key",
    "_signing_secret",
    "_token",
    "_verification_token",
    "_verify_token",
    "_webhook_secret",
)


def _normalized_key(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "_")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).lower()


def _is_private_key(value: Any) -> bool:
    key = _normalized_key(value)
    return (
        key in _PRIVATE_CONFIG_SECTIONS
        or key in _SENSITIVE_CONFIG_KEYS
        or any(key.endswith(suffix) for suffix in _SENSITIVE_CONFIG_SUFFIXES)
    )


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _sanitize_value(item)
            for key, item in value.items()
            if not _is_private_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def sanitize_public_tenant_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a detached tenant config safe for unauthenticated responses."""

    if not isinstance(config, Mapping):
        return {}
    return _sanitize_value(config)
