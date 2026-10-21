import hashlib
import uuid


def stable_demo_chat_session_id(demo_session_id: str | None) -> str:
    """Derive the short chat-session identifier bound to a signed demo token."""

    token = str(demo_session_id or "").strip()
    if not token:
        return str(uuid.uuid4())
    if len(token) <= 36:
        return token
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"sid_{digest}"
