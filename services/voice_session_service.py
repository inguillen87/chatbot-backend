from __future__ import annotations

import hashlib


def resolve_voice_chat_session_id(*, call_sid: str | None, from_number: str | None, to_number: str | None) -> str:
    """Deterministic voice session id resolver shared by voice modules."""
    if call_sid and len(call_sid) <= 36:
        return call_sid

    raw_id = f"voice_{from_number}_{to_number}_{call_sid}"
    return hashlib.md5(raw_id.encode()).hexdigest()
