import pytest
from unittest.mock import patch
from services.municipio_responder import GreetingHandler, CONTEXTO_MUNICIPIO

def test_greeting_handler_whatsapp_menu():
    # Create a mock context for WhatsApp
    context = {"channel": "whatsapp"}

    # Create an instance of the GreetingHandler
    handler = GreetingHandler(context)

    # Call the handle method
    response = handler.handle({})

    # Assert that the response is correct
    assert response is not None
    # When no user name is known, the bot should ask for it instead of using a
    # generic fallback like "vecino".
    assert "¿podrías decirme tu nombre?" in response.get("message_body", "")
    # The initial prompt doesn't include menu options until the user provides
    # their name, so options_list should be empty and fuente marks the request
    # for the name.
    assert response.get("options_list") is None or len(response.get("options_list", [])) == 0
    assert response.get("fuente") == "pedir_nombre_inicial"


def test_greeting_handler_preserves_profile_name():
    ctx_data = {"profile_name": "Mauricio", CONTEXTO_MUNICIPIO: {"user": {"id": 1}}}
    context = {"profile_name": "Mauricio", "chat_db_context_data": ctx_data}
    handler = GreetingHandler(context)
    response = handler.handle({})
    assert "Mauricio" in response.get("message_body", "")
    assert ctx_data.get("profile_name") == "Mauricio"
