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

    def _normalize_button_fields(button: MutableMapping) -> None:
        """Populate canonical keys for a button payload in-place."""

        # Only attempt to normalize structures that look like interactive
        # options.  Requiring at least one of these keys avoids touching other
        # dictionaries (e.g. chart data with ``label``/``value`` pairs).
        if not any(key in button for key in ("action_id", "action", "id", "texto")):
            return

        texto = button.get("texto")
        if not texto:
            for candidate in ("label", "title", "text", "name"):
                value = button.get(candidate)
                if value:
                    texto_candidate = str(value).strip()
                    if texto_candidate:
                        texto = texto_candidate
                        button["texto"] = texto_candidate
                        break
        elif not isinstance(texto, str):
            button["texto"] = str(texto)

        action_id = button.get("action_id")
        if not action_id:
            for candidate in ("id", "action", "value", "key"):
                value = button.get(candidate)
                if value:
                    action_id = value
                    button["action_id"] = value
                    break
            else:
                if texto:
                    button["action_id"] = texto
                    action_id = texto

        if action_id and "id" not in button:
            button["id"] = action_id

    def _normalize(obj: Any) -> None:
        if isinstance(obj, MutableMapping):
            _normalize_button_fields(obj)

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
