import unittest
from unittest.mock import patch, MagicMock
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipio_responder import responder_municipio

def test_greeting_handler(client):
    with patch('services.municipio_responder.GreetingHandler') as mock_greeting_handler:
        mock_handler_instance = mock_greeting_handler.return_value
        mock_handler_instance.handle.return_value = {
            "message_body": "Hola, bienvenido al test.",
            "fuente": "mocked_greeting"
        }
        response = responder_municipio(
            pregunta_original="hola", owner_user=MagicMock(),
            viewer_user=MagicMock(), chat_db_context=MagicMock(),
            rubro_obj=MagicMock(nombre='municipio')
        )
        mock_greeting_handler.assert_called_once()
        mock_handler_instance.handle.assert_called_once()
        assert response["message_body"] == "Hola, bienvenido al test."

def test_reclamo_handler_inicio(client):
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "Entendido, iniciando reclamo. ¿Sobre qué es?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "descripcion"
        }
        response = responder_municipio(
            pregunta_original="reclamo", owner_user=MagicMock(),
            viewer_user=MagicMock(), chat_db_context=MagicMock(context_data={}),
            rubro_obj=MagicMock(nombre='municipio')
        )
        mock_llamar_gemini.assert_called_once()
        assert response["message_body"] == "Entendido, iniciando reclamo. ¿Sobre qué es?"

@patch('services.municipio_responder.llamar_gemini')
def test_responder_municipio_imagen(mock_llamar_gemini, client):
    mock_llamar_gemini.return_value = {
        "accion_backend": "crear_reclamo",
        "datos_estructura": {"target": "municipio", "categoria": "Bacheo", "descripcion": "El usuario envió una imagen de un bache."},
        "message_body": ""
    }
    datos_interpretados = {
        "es_reclamo": True, "categoria_sugerida": "Bacheo",
        "descripcion_sugerida": "Parece ser un bache.",
        "ubicacion_sugerida": "Calle Falsa 123"
    }
    owner_user_mock = MagicMock(id=1, municipio_id=1, rubro=MagicMock(nombre='municipio'))
    viewer_user_mock = None
    chat_context_mock = MagicMock()
    chat_context_mock.context_data = {}

    with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
        response = responder_municipio(
            pregunta_original="Mira esta foto",
            owner_user=owner_user_mock,
            viewer_user=viewer_user_mock,
            chat_db_context=chat_context_mock,
            rubro_obj=owner_user_mock.rubro,
            datos_interpretados_archivo=datos_interpretados,
            anon_id="anon-test-image-claim"
        )

        mock_crear_ticket.assert_not_called()
        assert response["success"] is False
        assert "Para poder registrar tu reclamo" in response["message_to_user"]
        assert "nombre_completo" in response["pedir_info"]
        assert "telefono" in response["pedir_info"]
        assert "email" in response["pedir_info"]

def test_button_click_sets_category_and_advances_flow(client):
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "Entendido. Para el reclamo de Luminaria, por favor decime la descripción del problema y la dirección.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "descripcion_y_ubicacion"
        }
        chat_db_context = MagicMock(context_data={})
        response = responder_municipio(
            pregunta_original="reclamoluminaria",
            owner_user=MagicMock(id=1), viewer_user=None,
            chat_db_context=chat_db_context, rubro_obj=MagicMock(nombre='municipio'),
            channel="web", action="reclamo_luminaria"
        )
        assert "decime la descripción del problema y la dirección" in response["message_body"]
        contexto_guardado = chat_db_context.context_data.get("contexto_municipio_v2", {})
        datos_reclamo = contexto_guardado.get("datos_parciales_llm_reclamo", {})
        assert datos_reclamo.get("categoria") == "Luminaria"
        assert contexto_guardado.get("estado_conversacion") == "ESPERANDO_INFO_RECLAMO_LLM"
        assert contexto_guardado.get("esperando_info_llm_reclamo") == "descripcion_y_ubicacion"

if __name__ == '__main__':
    unittest.main()
