"""Helpers for logging connection metadata without leaking credentials."""

from __future__ import annotations

from sqlalchemy.engine.url import make_url


def _mask_identity(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def describe_database_uri(database_uri: str | None) -> str:
    """Return connection metadata with credentials and query values redacted."""

    if not database_uri:
        return "<unset>"

    try:
        url = make_url(str(database_uri))
        if url.drivername.startswith("sqlite"):
            database = str(url.database or "<memory>")
            return f"{url.drivername}:///{database}"

        auth = ""
        if url.username:
            auth = f"{_mask_identity(url.username)}:***@"

        host = url.host or "localhost"
        port = f":{url.port}" if url.port else ""
        database = f"/{url.database}" if url.database else ""
        query_keys = sorted(str(key) for key in url.query.keys())
        query = f"?{'&'.join(query_keys)}" if query_keys else ""
        return f"{url.drivername}://{auth}{host}{port}{database}{query}"
    except Exception:
        return "<configured>"
