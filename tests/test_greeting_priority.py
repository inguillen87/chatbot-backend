import pytest
from flask import Flask
from types import SimpleNamespace
from unittest.mock import patch

from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO


def test_greeting_skips_reclamo_detection():
    app = Flask(__name__)
    with app.app_context():
        owner = SimpleNamespace(id=1, municipio_id=1)
        rubro = SimpleNamespace()
        chat_ctx = SimpleNamespace(context_data={CONTEXTO_MUNICIPIO: {}}, chat_session_id="test")
        with patch("services.municipio_responder.ReclamoFlowHandler") as mock_reclamo, \
             patch("services.municipio_responder.cargar_configuracion_municipio", return_value=None), \
             patch("services.municipio_responder.flag_modified"):
            response = responder_municipio(
                "hola", owner, rubro, chat_db_context=chat_ctx, anon_id="anon", channel="whatsapp"
            )
            mock_reclamo.assert_not_called()
            assert response.get("fuente") == "greeting_handler_structured_menu_v2"
