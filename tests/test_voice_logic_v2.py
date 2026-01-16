import sys
import os
sys.path.append(os.getcwd())

from services.voice_handler import _clean_text_for_speech

def test_clean_text_for_speech_v2():
    raw = "Hola *mundo*! [link](http://google.com). ¿Todo bien?"
    cleaned = _clean_text_for_speech(raw)
    print(f"Original: {raw} -> Cleaned: {cleaned}")
    assert "*" not in cleaned
    assert "http" not in cleaned
    assert "mundo" in cleaned

    # Test conversational connector
    raw_q = "Si, claro."
    cleaned_q = _clean_text_for_speech(raw_q)
    # The helper itself doesn't add the question, handle_voice_interaction does.
    # We just check cleaning here.
    assert "Si, claro." in cleaned_q

if __name__ == "__main__":
    test_clean_text_for_speech_v2()
