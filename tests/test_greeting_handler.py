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
    # Check for the new generic greeting message
    assert "¡Hola! 👋 Soy JUNI" in response.get("message_body", "")
    # Check for the correct number of options in the WhatsApp menu
    assert len(response.get("options_list", [])) == 4
    # Check for the correct source
    assert response.get("fuente") == "greeting_handler_structured_menu_v2"


def test_greeting_handler_preserves_profile_name():
    ctx_data = {"profile_name": "Mauricio", CONTEXTO_MUNICIPIO: {"user": {"id": 1}}}
    context = {"profile_name": "Mauricio", "chat_db_context_data": ctx_data}
    handler = GreetingHandler(context)
    response = handler.handle({})
    assert "Mauricio" in response.get("message_body", "")
    assert ctx_data.get("profile_name") == "Mauricio"
