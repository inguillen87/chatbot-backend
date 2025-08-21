import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipio_responder import responder_municipio

def test_reclamo_handler_categoria_buttons(client):
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = (
            {
                "message_body": "Por favor, elegí una de las siguientes categorías:",
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {},
                "pedir_info": "categoria",
                "botones": [
                    {"texto": "Alumbrado Público", "action_id": "alumbrado_publico"},
                    {"texto": "Bacheo", "action_id": "bacheo"},
                    {"texto": "Recolección de Residuos", "action_id": "recoleccion_de_residuos"},
                ]
            },
            {}
        )
        owner_user = MagicMock()
        owner_user.id = 1
        rubro_obj = None
        viewer_user = None
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="quiero hacer un reclamo",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id",
            channel="whatsapp"
        )
        assert response is not None
        from services.response_formatter import build_interactive_response
        formatted_response = build_interactive_response(
            options=response.get('options_list', []),
            body_text=response.get('message_body'),
            channel='whatsapp',
            message_type='text',
            original_bot_response=response
        )
        assert formatted_response["text"]["body"] == "Por favor, elegí una de las siguientes categorías:\n\n*1*. Alumbrado Público\n*2*. Bacheo\n*3*. Recolección de Residuos\n\nResponde con el número de la opción que necesites."


def test_reclamo_handler_share_location_button(client):
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = (
            {
                "message_body": "Por favor, compartí tu ubicación para que podamos registrar el reclamo.",
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {},
                "pedir_info": "ubicacion",
                "botones": [
                    {"texto": "Compartir ubicación", "action_id": "compartir_ubicacion"}
                ]
            },
            {}
        )
        owner_user = MagicMock()
        owner_user.id = 1
        rubro_obj = None
        viewer_user = None
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="Poste de luz roto",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id",
            channel="whatsapp"
        )
        assert response is not None
        from services.response_formatter import build_interactive_response
        formatted_response = build_interactive_response(
            options=response.get('options_list', []),
            body_text=response.get('message_body'),
            channel='whatsapp',
            message_type='text',
            original_bot_response=response
        )
        assert formatted_response["text"]["body"] == "Por favor, compartí tu ubicación para que podamos registrar el reclamo.\n\n*1*. Compartir ubicación\n\nResponde con el número de la opción que necesites."


def test_ticket_status_handler_ticket_number_shortcut(client):
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = (
            {
                "message_body": "El ticket **M-12345** sobre 'Test' se encuentra en estado: **En Proceso**.",
                "accion_backend": "consultar_estado_ticket",
                "datos_estructura": {
                    "id_ticket_mencionado": "12345"
                },
                "pedir_info": None
            },
            {}
        )
        owner_user = MagicMock()
        owner_user.id = 1
        rubro_obj = None
        viewer_user = None
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="quiero saber el estado de mi ticket 12345",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id",
            channel="whatsapp"
        )
        assert response is not None
        assert "El ticket **M-12345** sobre 'Test' se encuentra en estado: **En Proceso**." in response["message_body"]

if __name__ == '__main__':
    unittest.main()
