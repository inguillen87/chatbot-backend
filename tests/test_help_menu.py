from services.common_utils import _get_main_menu_payload


def test_main_menu_includes_help_and_emojis_message():
    payload = _get_main_menu_payload({"channel": "whatsapp", "profile_name": "Test"})
    textos = [b["texto"] for b in payload["categorias"][0]["botones"]]
    assert any("Ayuda" in t for t in textos)
    assert "emojis" in payload["message_body"].lower()
