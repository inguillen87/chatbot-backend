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


def get_map_config() -> Dict[str, str]:
    """Expose the map provider configuration expected by modern frontends.

    The new dashboard can render Google Maps or MapLibre/MapTiler.  We publish
    the available credentials and hint which provider should be used so the FE
    avoids instantiating SDKs that are not configured (which produced runtime
    errors like ``Cannot read properties of undefined (reading 'Map')`` when
    Google Maps was not available).
    """

    google_key = _get_config_value("GOOGLE_MAPS_API_KEY")
    maptiler_key = _get_config_value("MAPTILER_API_KEY")
    # Allow overriding the style URL while keeping a sensible default for
    # MapLibre/MapTiler renders.
    style_url = (
        _get_config_value("MAPLIBRE_STYLE_URL")
        or _get_config_value("MAPTILER_STYLE_URL")
        or "https://api.maptiler.com/maps/streets/style.json"
    )

    if google_key:
        provider = "google"
    elif maptiler_key:
        provider = "maptiler"
    else:
        provider = "none"

    return {
        "provider": provider,
        "google_maps_key": google_key,
        "maptiler_key": maptiler_key,
        "style_url": style_url,
    }
