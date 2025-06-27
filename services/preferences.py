from flask import session


def _prefs() -> dict:
    """Return preferences dict from session, creating it if needed."""
    return session.setdefault("preferencias", {})


def add_preference(key: str, value: str) -> None:
    """Store a preference value under a key."""
    if not key or not value:
        return
    prefs = _prefs()
    valores = set(prefs.get(key, []))
    valores.add(value)
    prefs[key] = list(valores)
    if hasattr(session, "modified"):
        session.modified = True


def get_preferences(key: str | None = None):
    prefs = _prefs()
    if key is not None:
        return prefs.get(key, [])
    return prefs


def clear_preferences() -> None:
    session["preferencias"] = {}
    if hasattr(session, "modified"):
        session.modified = True
