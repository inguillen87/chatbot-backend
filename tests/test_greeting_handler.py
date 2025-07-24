import pytest
from unittest.mock import patch
from services.municipios import GreetingHandler

def test_greeting_handler():
    # Create a mock context
    context = {}

    # Create an instance of the GreetingHandler
    handler = GreetingHandler(context)

    # Call the handle method
    response = handler.handle({})

    # Assert that the response is correct
    assert response is not None
    assert "¡Hola! ¿En qué puedo ayudarte?" in response.get("message_body", "")
    assert len(response.get("options_list", [])) == 2
    assert response.get("message_type") == "interactive_buttons"
    assert response.get("fuente") == "greeting_handler_v2"
