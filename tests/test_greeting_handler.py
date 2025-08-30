import pytest
from unittest.mock import patch
from services.municipio_responder import GreetingHandler

def test_greeting_handler():
    # Create a mock context
    context = {}

    # Create an instance of the GreetingHandler
    handler = GreetingHandler(context)

    # Call the handle method
    response = handler.handle({})

    # Assert that the response is correct
    assert response is not None
    # Check for the new generic greeting message
    assert "¡Hola! 👋 Soy JUNI" in response.get("message_body", "")
    # Check for the correct number of options in the new menu
    assert len(response.get("options_list", [])) == 4
    # Check for the correct source
    assert response.get("fuente") == "greeting_handler_structured_menu_v2"
