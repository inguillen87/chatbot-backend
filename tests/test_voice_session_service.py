from services.voice_session_service import resolve_voice_chat_session_id


def test_voice_session_resolver_prefers_short_call_sid():
    assert resolve_voice_chat_session_id(call_sid="CA123", from_number="1", to_number="2") == "CA123"


def test_voice_session_resolver_hashes_long_sid():
    sid = "X" * 80
    out = resolve_voice_chat_session_id(call_sid=sid, from_number="+1", to_number="+2")
    assert len(out) == 32
    assert out == resolve_voice_chat_session_id(call_sid=sid, from_number="+1", to_number="+2")
