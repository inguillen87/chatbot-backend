"""Configuration helpers for the analytics module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from flask import current_app


@dataclass
class AnalyticsConfig:
    cache_ttl_seconds: int
    max_cache_items: int
    feature_enabled: bool

    @property
    def cache_ttl(self) -> timedelta:
        return timedelta(seconds=self.cache_ttl_seconds)


def get_config() -> AnalyticsConfig:
    app = current_app
    ttl = int(app.config.get("ANALYTICS_CACHE_TTL", 600))
    max_items = int(app.config.get("ANALYTICS_CACHE_MAX_ITEMS", 512))
    enabled = bool(app.config.get("ANALYTICS_ENABLED", True))
    return AnalyticsConfig(ttl, max_items, enabled)
