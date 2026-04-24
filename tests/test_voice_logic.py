from services.voice_handler import _clean_text_for_speech

def test_clean_text_for_speech():
    raw = "Hola *mundo*! Hacé click en http://google.com 😊"
    cleaned = _clean_text_for_speech(raw)
    assert "mundo" in cleaned
    assert "*" not in cleaned
    assert "http" not in cleaned
    assert "Selecciona" in cleaned
    # Emoji removal check (basic range)
    assert "😊" not in cleaned
    print(f"Original: {raw} -> Cleaned: {cleaned}")
    print("Test passed!")

if __name__ == "__main__":
    test_clean_text_for_speech()
