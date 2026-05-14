from models import ChatSessionContext, db
from routes.chat import _normalize_chat_session_id


def test_normalize_chat_session_id_keeps_uuid_values():
    raw = "2abc22ae-17c4-4f34-8b54-8ddf1a5be16f"

    assert _normalize_chat_session_id(raw) == raw


def test_normalize_chat_session_id_hashes_overlong_tokens():
    raw = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + ("x" * 120)

    normalized = _normalize_chat_session_id(raw)

    assert normalized.startswith("sid_")
    assert len(normalized) == 36
    assert normalized == _normalize_chat_session_id(raw)


def test_normalized_overlong_chat_session_id_fits_context_pk(client):
    raw = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + ("x" * 120)
    normalized = _normalize_chat_session_id(raw)

    with client.application.app_context():
        db.session.add(
            ChatSessionContext(
                chat_session_id=normalized,
                context_data={"source_chat_session_id": raw},
            )
        )
        db.session.commit()

        saved = db.session.get(ChatSessionContext, normalized)

    assert saved is not None
    assert saved.context_data["source_chat_session_id"] == raw
