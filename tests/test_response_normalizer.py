from utils.response_utils import normalize_response_payload


def test_normalize_response_payload_maps_legacy_fields():
    payload = {
        "message_to_user": "Hola",
        "botones": [{"texto": "Opción"}],
        "pedir_info": "email",
        "success": False,
    }

    normalized = normalize_response_payload(payload)

    assert normalized["message_body"] == "Hola"
    assert normalized["options_list"][0]["texto"] == "Opción"
    assert normalized["message_type"] == "interactive_buttons"
    assert normalized["success"] is True
