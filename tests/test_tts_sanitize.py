from services.tts_orchestrator import sanitize_for_tts


def test_sanitize_newlines_to_periods():
    text = "Hola\ncomo estas"
    result = sanitize_for_tts(text)
    assert result == "Hola. como estas"


def test_sanitize_bullet_points():
    text = "- uno\n- dos"
    result = sanitize_for_tts(text)
    assert result.startswith("Punto: uno. Punto: dos")


def test_sanitize_long_text_truncates():
    text = "hola " * 200
    result = sanitize_for_tts(text)
    assert len(result) <= 500
