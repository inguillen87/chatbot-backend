import pytest
from flask import Flask
from types import SimpleNamespace
from unittest.mock import patch

from services.municipio_responder import (
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    ConversationState,
    clear_municipio_cache,
)


def test_greeting_skips_reclamo_detection():
    app = Flask(__name__)
    with app.app_context():
        owner = SimpleNamespace(id=1, municipio_id=1)
        rubro = SimpleNamespace()
        chat_ctx = SimpleNamespace(
            context_data={CONTEXTO_MUNICIPIO: {}, "profile_name": "Test"},
            chat_session_id="test",
        )
        with patch("services.municipio_responder.ReclamoFlowHandler") as mock_reclamo, \
             patch("services.municipio_responder.cargar_configuracion_municipio", return_value=None), \
             patch("services.municipio_responder.flag_modified"):
            response = responder_municipio(
                "hola",
                owner,
                rubro,
                chat_db_context=chat_ctx,
                anon_id="anon",
                channel="whatsapp",
                profile_name="Test",
            )
        mock_reclamo.assert_not_called()
        assert response.get("fuente") in {"greeting_handler_structured_menu_v2", "pedir_nombre_inicial"}


def test_greeting_cache_updates_when_contact_known():
    app = Flask(__name__)
    with app.app_context():
        owner = SimpleNamespace(id=1, municipio_id=1)
        rubro = SimpleNamespace()
        chat_ctx = SimpleNamespace(
            context_data={CONTEXTO_MUNICIPIO: {}}, chat_session_id="cache-test"
        )

        clear_municipio_cache()

        with patch("services.municipio_responder.cargar_configuracion_municipio", return_value=None), \
             patch("services.municipio_responder.flag_modified"):
            # Initial greeting should request the user's name and populate the cache.
            response_1 = responder_municipio(
                "hola", owner, rubro, chat_db_context=chat_ctx, anon_id="anon", channel="web"
            )
            assert response_1.get("fuente") == "pedir_nombre_inicial"

            # Simulate that the conversation now knows the contact's name and is ready for the menu.
            municipal_ctx = chat_ctx.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
            municipal_ctx["contacto_usuario"] = {"nombre": "Marcelo"}
            municipal_ctx["estado_conversacion"] = (
                ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
            )

            response_2 = responder_municipio(
                "hola", owner, rubro, chat_db_context=chat_ctx, anon_id="anon", channel="web"
            )

            assert response_2.get("fuente") == "greeting_handler_structured_menu_v2"
            assert "Marcelo" in response_2.get("message_body", "")
