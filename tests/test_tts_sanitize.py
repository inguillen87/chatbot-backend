from services.tts_orchestrator import sanitize_for_tts


def test_sanitize_newlines_to_periods():
    text = "Hola\ncomo estas"
    result = sanitize_for_tts(text)
    assert result == "Hola. como estas"
