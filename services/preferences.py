# from flask import session # REMOVED
from typing import Dict, Any

PREFERENCES_KEY = "preferencias"
AUDIO_ENABLED_KEY = "audio_enabled"

def _prefs(chat_context_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return preferences dict from chat_context_data, creating it if needed."""
    return chat_context_data.setdefault(PREFERENCES_KEY, {})


def add_preference(chat_context_data: Dict[str, Any], key: str, value: str) -> None:
    """Store a preference value under a key in chat_context_data."""
    if not key or not value:
        return
    prefs = _prefs(chat_context_data)
    # Ensure that existing values are treated as a list/set
    current_values_for_key = prefs.get(key)
    if not isinstance(current_values_for_key, list):
        # If it's not a list (e.g., first time, or was a single string), re-initialize or handle
        # For simplicity, if it's not a list, we start fresh or assume it should have been a list.
        # A more robust approach might try to convert a single string value to a list.
        current_values_for_key = []

    valores = set(current_values_for_key) # Convert to set for easy addition
    valores.add(value)
    prefs[key] = list(valores)
    # No session.modified = True needed; chat_context_data persistence is handled by caller


def get_preferences(chat_context_data: Dict[str, Any], key: str | None = None) -> Any:
    """Get preferences from chat_context_data."""
    prefs = _prefs(chat_context_data)
    if key is not None:
        return prefs.get(key, [])
    return prefs


def set_audio_enabled(chat_context_data: Dict[str, Any], enabled: bool) -> None:
    """Enable or disable audio responses in chat_context_data."""
    prefs = _prefs(chat_context_data)
    prefs[AUDIO_ENABLED_KEY] = enabled


def is_audio_enabled(chat_context_data: Dict[str, Any], user: Any | None = None) -> bool:
    """Return True if audio responses should be generated."""
    if chat_context_data.get("source_is_audio"):
        return True

    prefs = _prefs(chat_context_data)
    context_pref = prefs.get(AUDIO_ENABLED_KEY)
    if isinstance(context_pref, bool):
        return context_pref

    if user is not None and getattr(user, "prefers_audio", False):
        return True

    return False


def clear_preferences(chat_context_data: Dict[str, Any]) -> None:
    """Clear preferences in chat_context_data."""
    chat_context_data[PREFERENCES_KEY] = {}
    # No session.modified = True
