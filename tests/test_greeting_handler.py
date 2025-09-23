import pytest
from types import SimpleNamespace
from unittest.mock import patch
from services.constants import ConversationState
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


def test_greeting_handler_uses_contacto_usuario_name():
    ctx_data = {
        CONTEXTO_MUNICIPIO: {
            "contacto_usuario": {"nombre": "Marcelo"},
            "estado_conversacion": "ESPERANDO_SELECCION_MENU_PRINCIPAL",
        }
    }
    context = {"chat_db_context_data": ctx_data}
    handler = GreetingHandler(context)
    response = handler.handle({})

    # The greeting should incorporate the stored contact name instead of asking for it again.
    assert "Marcelo" in response.get("message_body", "")
    assert response.get("fuente") != "pedir_nombre_inicial"


def test_greeting_handler_treats_owner_as_anonymous_viewer():
    owner = SimpleNamespace(id=4, rol="admin", municipio_id=10, name="Mauricio")
    viewer = SimpleNamespace(id=4, rol="admin", municipio_id=10, name="Mauricio")
    context = {
        "user_obj": owner,
        "viewer_user_obj": viewer,
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: {}},
        "session_kind": "widget",
    }

    handler = GreetingHandler(context)
    response = handler.handle({})

    assert response.get("fuente") == "pedir_nombre_inicial"
    assert "¿podrías decirme tu nombre?" in response.get("message_body", "").lower()
    municipal_ctx = context["chat_db_context_data"].get(CONTEXTO_MUNICIPIO, {})
    assert (
        municipal_ctx.get("estado_conversacion")
        == ConversationState.ESPERANDO_NOMBRE_INICIAL.name
    )
