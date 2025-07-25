import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipios import responder_municipio

def test_greeting_handler(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "¡Hola! ¿En qué puedo ayudarte?",
            "accion_backend": "saludar",
            "datos_estructura": {},
            "pedir_info": None,
            "botones": [
                {"texto": "Hacer un reclamo", "id_accion": "iniciar_reclamo"},
                {"texto": "Consultar estado de un trámite", "id_accion": "info_tramite"}
            ]
        }
        owner_user = MagicMock()
        owner_user.id = 1
        rubro_obj = None
        viewer_user = None
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="hola",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id"
        )
        assert response is not None
        assert "¡Hola! ¿En qué puedo ayudarte?" in response["message_body"]

def test_reclamo_handler_inicio(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Lamento que estés teniendo un problema. ¿Me podrías describir la situación?",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {},
            "pedir_info": "descripcion"
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
            anon_id="test_anon_id"
        )
        assert response is not None
        assert "Lamento que estés teniendo un problema." in response["message_body"]

def test_responder_municipio_imagen(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        with patch('services.municipios.process_image_for_chat_task.delay') as mock_process_image_task:
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "He recibido tu imagen y la estoy analizando. Te enviaré un mensaje cuando termine.",
                "accion_backend": "analizar_imagen",
                "datos_estructura": {},
                "pedir_info": None
            }
            pregunta_original = ""
            owner_user = MagicMock()
            owner_user.id = 1
            rubro_obj = None
            viewer_user = None
            chat_db_context = MagicMock()
            chat_db_context.context_data = {}

            kwargs = {
                "chat_session_uuid": "test_session_uuid",
                "uploaded_file_info_whatsapp": {
                    "url": "http://example.com/imagen.jpg",
                    "mime_type": "image/jpeg",
                    "source": "whatsapp",
                },
            }

            response = responder_municipio(
                pregunta_original,
                owner_user,
                rubro_obj,
                viewer_user,
                chat_db_context,
                "test_anon_id",
                **kwargs,
            )

            assert response is not None
            assert "He recibido tu imagen y la estoy analizando." in response["message_body"]

if __name__ == '__main__':
    unittest.main()
