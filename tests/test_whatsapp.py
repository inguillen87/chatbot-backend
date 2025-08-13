import unittest
from unittest.mock import patch, MagicMock
from services.municipio_responder import responder_municipio
import pytest

@pytest.fixture
def mock_tts():
    """Mock para el servicio GoogleTextToSpeechService."""
    with patch('services.google_text_to_speech.TextToSpeechService') as mock:
        yield mock

@pytest.mark.legacy
def test_reclamo_handler_categoria_buttons(init_database, owner_user, viewer_user, rubro, mock_tts):
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "Por favor, elegí una de las siguientes categorías:",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {},
            "pedir_info": "categoria",
            "botones": [
                {"texto": "Alumbrado Público", "action_id": "alumbrado_publico"},
                {"texto": "Bacheo", "action_id": "bacheo"},
                {"texto": "Recolección de Residuos", "action_id": "recoleccion_de_residuos"},
            ]
        }
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="quiero hacer un reclamo",
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id",
            channel="whatsapp"
        )
        assert response is not None
        from services.response_formatter import build_interactive_response
        formatted_response = build_interactive_response(
            options=response.get('botones', []),
            body_text=response.get('message_body'),
            channel='whatsapp',
        )
        assert formatted_response["text"]["body"] == "Por favor, elegí una de las siguientes categorías:\n\n*1*. Alumbrado Público\n*2*. Bacheo\n*3*. Recolección de Residuos\n\n\n\nResponde con el número de la opción que necesites."


@pytest.mark.legacy
def test_reclamo_handler_share_location_button(init_database, owner_user, viewer_user, rubro, mock_tts):
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "Por favor, compartí tu ubicación para que podamos registrar el reclamo.",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {},
            "pedir_info": "ubicacion",
            "botones": [
                {"texto": "Compartir ubicación", "action_id": "compartir_ubicacion"}
            ]
        }
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="Poste de luz roto",
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id",
            channel="whatsapp"
        )
        assert response is not None
        from services.response_formatter import build_interactive_response
        formatted_response = build_interactive_response(
            options=response.get('botones', []),
            body_text=response.get('message_body'),
            channel='whatsapp',
        )
        assert formatted_response["text"]["body"] == "Por favor, compartí tu ubicación para que podamos registrar el reclamo.\n\n*1*. Compartir ubicación\n\n\n\nResponde con el número de la opción que necesites."


@pytest.mark.legacy
def test_ticket_status_handler_ticket_number_shortcut(init_database, owner_user, viewer_user, rubro, mock_tts):
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "El ticket **M-12345** sobre 'Test' se encuentra en estado: **En Proceso**.",
            "accion_backend": "consultar_estado_ticket",
            "datos_estructura": {
                "id_ticket_mencionado": "12345"
            },
            "pedir_info": None
        }
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="quiero saber el estado de mi ticket 12345",
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id",
            channel="whatsapp"
        )
        assert response is not None
        assert "El ticket **M-12345** sobre 'Test' se encuentra en estado: **En Proceso**." in response["message_body"]

if __name__ == '__main__':
    unittest.main()
