import pytest
from unittest.mock import patch
from services.municipio_responder import (
    GreetingHandler,
    CONTEXTO_MUNICIPIO,
    handle_llm_interaction,
    ConversationState,
)
import time

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


@patch("services.municipio_responder.llamar_llm_con_fallback")
@patch("services.municipio_responder.GreetingHandler")
def test_saludo_ignorado_en_flujo_activo(mock_handler, mock_llm):
    mock_llm.return_value = ({"message_body": "Hola", "accion_backend": "saludar"}, {})
    contexto = {"estado_conversacion": ConversationState.ESPERANDO_DIRECCION_RECLAMO.name}
    response, updated = handle_llm_interaction(
        None, "hola", {}, None, None, None, contexto
    )
    mock_handler.assert_not_called()
    assert updated["estado_conversacion"] == ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
    # response should be None to trigger fallback or re-prompt
    assert response is None


def test_greeting_skips_after_recent_ticket():
    ctx = {
        CONTEXTO_MUNICIPIO: {
            "last_system_event": {"type": "ticket_created", "ts": time.time()}
        },
        "chat_db_context_data": {},
    }
    handler = GreetingHandler(ctx)
    assert handler.handle({}) is None
