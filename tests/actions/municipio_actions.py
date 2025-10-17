"""Compatibility shims for legacy tests expecting test-local municipio_actions helpers."""
from services.actions.municipio_actions import (
    CrearReclamoActionHandler,
    _normalize_url_for_comparison,
)

__all__ = ["CrearReclamoActionHandler", "_normalize_url_for_comparison"]
