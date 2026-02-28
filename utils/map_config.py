"""Utilities to expose map provider configuration for the frontend."""

from __future__ import annotations

import os
from typing import Dict

from flask import current_app


def _get_config_value(name: str) -> str:
    """Return a config value preferring Flask config over environment variables."""

    if name in current_app.config:
        value = current_app.config.get(name)
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value)

    env_value = os.getenv(name, "")
    return env_value.strip()


DEFAULT_STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"


def _normalize_style_url(style_url: str, maptiler_key: str) -> str:
    """Return a style URL that always works with MapTiler hosted styles.

    The MapTiler CDN requires the API key as query parameter.  Some
    installations relied on the default style URL provided here without the
    ``?key=`` suffix which broke MapLibre renders in production.  We append the
    key when talking to MapTiler and gracefully support custom URLs that embed
    a ``{key}`` placeholder.
    """

    style_url = (style_url or "").strip()
    if not style_url:
        return ""

    if "{key}" in style_url:
        # Allow templates like ``https://example/style.json?token={key}``
        return style_url.replace("{key}", maptiler_key)

    if not maptiler_key:
        return style_url

    if "api.maptiler.com" in style_url and "key=" not in style_url:
        separator = "&" if "?" in style_url else "?"
        return f"{style_url}{separator}key={maptiler_key}"

    return style_url


def get_map_config() -> Dict[str, str]:
    """Expose the map provider configuration expected by modern frontends.

    The new dashboard can render Google Maps or MapLibre/MapTiler.  We publish
    the available credentials and hint which provider should be used so the FE
    avoids instantiating SDKs that are not configured (which produced runtime
    errors like ``Cannot read properties of undefined (reading 'Map')`` when
    Google Maps was not available).  ``MAP_PROVIDER`` can be set to ``google``
    or ``maptiler`` to override the automatic provider selection when both keys
    are configured and we need to force one of them.
    """

    google_key = _get_config_value("GOOGLE_MAPS_API_KEY")
    maptiler_key = _get_config_value("MAPTILER_API_KEY")
    preferred_provider = _get_config_value("MAP_PROVIDER").lower()
    # Allow overriding the style URL while keeping a sensible default for
    # MapLibre/MapTiler renders.
    style_url = _normalize_style_url(
        _get_config_value("MAPLIBRE_STYLE_URL")
        or _get_config_value("MAPTILER_STYLE_URL")
        or DEFAULT_STYLE_URL,
        maptiler_key,
    )

    provider = "none"
    available_providers = []

    if google_key:
        available_providers.append("google")

    # MapLibre can run with a public style even when MAPTILER_API_KEY is absent,
    # so we advertise it as available whenever we have a style URL.
    if style_url:
        available_providers.append("maplibre")

    if preferred_provider in {"google", "maptiler", "maplibre"}:
        if preferred_provider == "google" and google_key:
            provider = "google"
        elif preferred_provider in {"maptiler", "maplibre"} and style_url:
            provider = "maplibre"
    else:
        if google_key:
            provider = "google"
        elif style_url:
            provider = "maplibre"

    return {
        "provider": provider,
        "provider_aliases": {"maptiler": "maplibre"},
        "available_providers": available_providers,
        "google_maps_key": google_key,
        "maptiler_key": maptiler_key,
        "style_url": style_url,
    }
