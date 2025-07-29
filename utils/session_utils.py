import uuid

def get_global_session_id(phone: str | None = None, email: str | None = None) -> str:
    """Return a consistent chat session ID across channels."""
    if phone:
        return f"global_{phone}"
    if email:
        return f"global_{email}"
    return f"anon_{uuid.uuid4()}"
