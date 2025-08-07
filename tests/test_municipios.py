import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipio_responder import responder_municipio

def test_greeting_handler(client):
    # Mockear el manejador de saludos para que no dependa de la configuración de Junín
    with patch('services.municipio_responder.GreetingHandler') as mock_greeting_handler:
        mock_handler_instance = mock_greeting_handler.return_value
        mock_handler_instance.handle.return_value = {
            "message_body": "Hola, bienvenido al test.",
            "fuente": "mocked_greeting"
        }

        # Simular una solicitud con un input de saludo
        response = responder_municipio(
            pregunta_original="hola",
            owner_user=MagicMock(),
            viewer_user=MagicMock(),
            chat_db_context=MagicMock(),
            rubro_obj=MagicMock(nombre='municipio')
        )

        # Verificar que el manejador de saludos fue llamado
        mock_greeting_handler.assert_called_once()
        mock_handler_instance.handle.assert_called_once()

        # Verificar que la respuesta es la que esperábamos del mock
        assert response["message_body"] == "Hola, bienvenido al test."
        assert response["fuente"] == "mocked_greeting"


def test_reclamo_handler_inicio(client):
    with patch('services.municipio_responder.ReclamoHandler') as mock_reclamo_handler:
        mock_handler_instance = mock_reclamo_handler.return_value
        mock_handler_instance.handle.return_value = {
            "message_body": "Iniciando reclamo de test.",
            "fuente": "mocked_reclamo"
        }

        # Simular una solicitud para iniciar un reclamo
        response = responder_municipio(
            pregunta_original="reclamo",
            owner_user=MagicMock(),
            viewer_user=MagicMock(),
            chat_db_context=MagicMock(),
            rubro_obj=MagicMock(nombre='municipio')
        )

        mock_reclamo_handler.assert_called_once()
        mock_handler_instance.handle.assert_called_once()

        assert response["message_body"] == "Iniciando reclamo de test."
        assert response["fuente"] == "mocked_reclamo"

def test_responder_municipio_imagen(client):
    with patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat') as mock_interpretar:
        mock_interpretar.return_value = {
            "es_reclamo": True,
            "categoria_sugerida": "Bacheo",
            "descripcion_sugerida": "Parece ser un bache."
        }

        response = responder_municipio(
            pregunta_original="Mira esta foto",
            owner_user=MagicMock(),
            viewer_user=MagicMock(),
            chat_db_context=MagicMock(),
            rubro_obj=MagicMock(nombre='municipio'),
            # Simular datos de imagen adjunta
            attachment_info={'url': 'http://example.com/img.png', 'mime_type': 'image/png'}
        )

        mock_interpretar.assert_called_once()
        assert "categoria_sugerida" in response
        assert response["categoria_sugerida"] == "Bacheo"

if __name__ == '__main__':
    unittest.main()
