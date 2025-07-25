import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipios import responder_municipio

def test_reclamo_handler_categoria_buttons(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Por favor, elegí una de las siguientes categorías:",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {},
            "pedir_info": "categoria",
            "botones": [
                {"texto": "Alumbrado Público", "id_accion": "alumbrado_publico"},
                {"texto": "Bacheo", "id_accion": "bacheo"},
                {"texto": "Recolección de Residuos", "id_accion": "recoleccion_de_residuos"},
            ]
        }
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
        assert response["message_type"] == "interactive_buttons"
        assert len(response["options_list"]) > 0

def test_reclamo_handler_share_location_button(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Por favor, compartí tu ubicación para que podamos registrar el reclamo.",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {},
            "pedir_info": "ubicacion",
            "botones": [
                {"texto": "Compartir ubicación", "id_accion": "compartir_ubicacion"}
            ]
        }
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
        assert response["message_type"] == "interactive_buttons"

def test_ticket_status_handler_ticket_number_shortcut(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "El ticket **M-12345** sobre 'Test' se encuentra en estado: **En Proceso**.",
            "accion_backend": "consultar_estado_ticket",
            "datos_estructura": {
                "id_ticket_mencionado": "12345"
            },
            "pedir_info": None
        }
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
