"""Utility helpers for normalizing chatbot responses for different clients."""
from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from typing import Any


def ensure_buttons_compatibility(payload: Any) -> Any:
    """Ensure ``botones`` and ``options_list`` keys are mirrored in a payload.

    Many parts of the backend historically produced ``options_list`` while the
    web widget expects ``botones``.  Conversely, some newer flows only set the
    ``botones`` key.  This helper mutates the provided payload (if it is a
    mapping) so both keys are present whenever either is available.  Nested
    dictionaries and sequences are processed recursively to keep the structure
    consistent across the entire response.

    Parameters
    ----------
    payload:
        Arbitrary object that may contain chatbot response data.

    Returns
    -------
    Any
        The same object that was passed in, allowing call-sites to use the
        helper inline within expressions.
    """

    def _normalize(obj: Any) -> None:
        if isinstance(obj, MutableMapping):
            opciones = obj.get("options_list")
            botones = obj.get("botones")

            if botones is None and isinstance(opciones, list):
                obj["botones"] = opciones
            elif opciones is None and isinstance(botones, list):
                obj["options_list"] = botones

            for value in obj.values():
                _normalize(value)
        elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            for item in obj:
                _normalize(item)

    _normalize(payload)
    return payload
