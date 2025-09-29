"""Simple in-process TTL cache tailored for analytics responses."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Hashable, Optional


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


class TTLCache:
    """A threadsafe TTL cache with optional max size.

    The analytics module intentionally keeps a light-weight cache to avoid
    adding dependencies while honouring the 5–15 minute caching window
    requested in the specification. Items are evicted lazily when accessed and
    the cache exposes simple stats for observability.
    """

    def __init__(self, ttl_seconds: int = 600, max_items: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._store: Dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._stats = CacheStats()

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def _purge_if_needed(self) -> None:
        if len(self._store) <= self.max_items:
            return
        # Remove the oldest entries until we fall below the threshold.
        sorted_items = sorted(self._store.items(), key=lambda item: item[1][0])
        overshoot = len(self._store) - self.max_items
        for key, _ in sorted_items[:overshoot]:
            self._store.pop(key, None)
            self._stats.evictions += 1

    def get(self, key: Hashable) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                self._stats.misses += 1
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._store.pop(key, None)
                self._stats.evictions += 1
                self._stats.misses += 1
                return None
            self._stats.hits += 1
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            expires_at = time.time() + self.ttl_seconds
            self._store[key] = (expires_at, value)
            self._purge_if_needed()

    def get_or_set(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.set(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._stats = CacheStats()


# Module-level cache instance reused by the blueprint.
analytics_cache = TTLCache()
