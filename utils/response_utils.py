"""Utility helpers for normalizing chatbot responses for different clients."""
from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from typing import Any


def ensure_buttons_compatibility(payload: Any) -> Any:
    """Normalize chatbot response payloads for web clients.

    Besides mirroring ``botones``/``options_list`` collections, some responses
    only populate ``message_body`` while others set ``respuesta`` or the legacy
    ``respuesta_usuario`` field.  The web widget expects all of them to be
    available.  This helper mutates the provided
    payload (when it is a mapping) so that:

    * ``message_body`` and ``respuesta`` mirror any available text.
    * interactive options expose both ``botones`` and ``options_list`` entries
      with normalized button fields.

    Nested dictionaries and sequences are processed recursively to keep the
    structure consistent across the entire response.

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

        url_value = button.get("url")
        if isinstance(url_value, str) and url_value.strip():
            button.setdefault("target", "_blank")
            button.setdefault("rel", "noopener noreferrer")

    def _mirror_text_fields(container: MutableMapping) -> None:
        """Ensure all text aliases share the same content."""

        message_body = container.get("message_body")
        respuesta = container.get("respuesta")
        respuesta_usuario = container.get("respuesta_usuario")
        message_to_user = container.get("message_to_user")

        def _has_content(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            return True

        def _coerce(value: Any) -> str:
            return value if isinstance(value, str) else str(value)

        canonical_text: str | None = None
        for candidate in (message_body, respuesta, respuesta_usuario, message_to_user):
            if _has_content(candidate):
                canonical_text = _coerce(candidate)
                break

        if canonical_text is None:
            return

        if not _has_content(message_body):
            container["message_body"] = canonical_text
        if not _has_content(respuesta):
            container["respuesta"] = canonical_text
        if not _has_content(respuesta_usuario):
            container["respuesta_usuario"] = canonical_text
        if not _has_content(message_to_user):
            container["message_to_user"] = canonical_text

    def _normalize(obj: Any) -> None:
        if isinstance(obj, MutableMapping):
            _normalize_button_fields(obj)
            _mirror_text_fields(obj)

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


def normalize_response_payload(payload: Any) -> Any:
    """Normalize response payloads to the standard contract."""

    ensure_buttons_compatibility(payload)

    if not isinstance(payload, MutableMapping):
        return payload

    if payload.get("message_to_user") and not payload.get("message_body"):
        payload["message_body"] = payload["message_to_user"]

    if payload.get("options_list") and not payload.get("botones"):
        payload["botones"] = payload.get("options_list")
    if payload.get("botones") and not payload.get("options_list"):
        payload["options_list"] = payload.get("botones")

    if "message_body" not in payload or payload.get("message_body") is None:
        payload["message_body"] = ""

    options_list = payload.get("options_list")
    if options_list is None:
        payload["options_list"] = []
    elif not isinstance(options_list, list):
        payload["options_list"] = list(options_list) if isinstance(options_list, Sequence) else []

    if "message_type" not in payload:
        payload["message_type"] = "interactive_buttons" if payload.get("options_list") else "text"

    success_value = payload.get("success")
    if success_value is None:
        payload["success"] = True
    elif payload.get("pedir_info") and success_value is False:
        payload["success"] = True

    payload.setdefault("data", {})
    payload.setdefault("image_url", None)
    payload.setdefault("audio_url", None)

    return payload
