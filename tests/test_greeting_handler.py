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
    assert "¡Hola! Soy JuniA, el asistente virtual de la Municipalidad de Junín." in response.get("message_body", "")
    assert len(response.get("categorias", [])) == 4
    assert response.get("fuente") == "greeting_handler_categorized_v1"
