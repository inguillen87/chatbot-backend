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
            chat_db_context=MagicMock(context_data={}),
            rubro_obj=MagicMock(nombre='municipio')
        )

        # Verificar que el manejador de saludos fue llamado
        mock_greeting_handler.assert_called_once()
        mock_handler_instance.handle.assert_called_once()

        # Verificar que la respuesta es la que esperábamos del mock
        assert response["message_body"] == "Hola, bienvenido al test."
        assert response["fuente"] == "mocked_greeting"


def test_reclamo_handler_inicio(client):
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = (
            {
                "message_body": "Entendido, iniciando reclamo. ¿Sobre qué es?",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"target": "municipio"},
                "pedir_info": "descripcion"
            },
            {}
        )

        # Simular una solicitud para iniciar un reclamo
        response = responder_municipio(
            pregunta_original="reclamo",
            owner_user=MagicMock(),
            viewer_user=MagicMock(),
            chat_db_context=MagicMock(context_data={}),
            rubro_obj=MagicMock(nombre='municipio')
        )

        mock_llamar_gemini.assert_called_once()
        assert response["message_body"] == "Entendido, iniciando reclamo. ¿Sobre qué es?"

@patch('services.municipio_responder.llamar_gemini')
def test_responder_municipio_imagen(mock_llamar_gemini, client):
    mock_llamar_gemini.return_value = (
        {
            "message_body": "Gracias por la imagen. Parece un reclamo sobre Bacheo. ¿Es correcto?",
            "accion_backend": "confirmar_reclamo_auto",
            "datos_estructura": {"categoria": "Bacheo", "descripcion": "Parece ser un bache."}
        },
        {}
    )

    datos_interpretados = {
        "es_reclamo": True,
        "categoria_sugerida": "Bacheo",
        "descripcion_sugerida": "Parece ser un bache.",
        "ubicacion_sugerida": "Calle Falsa 123"
    }

    # Mock the owner_user to have a valid municipio_id for the action handler
    owner_user_mock = MagicMock(id=1, municipio_id=1)

    with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
        mock_ticket = MagicMock()
        mock_ticket.nro_ticket = "IMG-001"
        mock_crear_ticket.return_value = mock_ticket

        response = responder_municipio(
            pregunta_original="Mira esta foto",
            owner_user=owner_user_mock,
            viewer_user=MagicMock(),
            chat_db_context=MagicMock(context_data={}),
            rubro_obj=MagicMock(nombre='municipio'),
            datos_interpretados_archivo=datos_interpretados
        )

        # The flow now asks for contact details since none were provided, which is correct.
        # In this specific flow, responder_municipio returns the dictionary directly.
        assert "Para continuar, aún necesito estos datos: ubicación, nombre, teléfono, email." in response["message_body"]

def test_button_click_sets_category_and_advances_flow(client):
    """
    Tests that clicking a sub-category button correctly sets the category
    in the context and advances the conversation to the next step.
    """
    with patch('services.municipio_responder.llamar_gemini') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = (
            {
                "message_body": "Entendido. Para el reclamo de Luminaria, por favor decime la descripción del problema y la dirección.",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"target": "municipio"},
                "pedir_info": "descripcion_y_ubicacion"
            },
            {}
        )

        chat_db_context = MagicMock(context_data={})

        # Simulate the user clicking the "Luminaria" button
        response = responder_municipio(
            pregunta_original="reclamoluminaria",
            owner_user=MagicMock(id=1),
            viewer_user=None,
            chat_db_context=chat_db_context,
            rubro_obj=MagicMock(nombre='municipio'),
            channel="web",
            action="reclamo_luminaria" # This is what the frontend sends
        )

        # 1. Assert the bot's response asks for the next piece of info
        assert "decime la descripción del problema y la dirección" in response["message_body"]

        # 2. Assert that the context was updated correctly
        contexto_guardado = chat_db_context.context_data.get("contexto_municipio_v2", {})
        datos_reclamo = contexto_guardado.get("datos_parciales_llm_reclamo", {})

        # The new logic with the intent classifier correctly sets the category from the action
        assert datos_reclamo.get("categoria") == "Luminaria"
        # And then it calls the LLM, which is what we are mocking.
        # The state is advanced inside the LLM handler, so we check the result of that.
        assert contexto_guardado.get("estado_conversacion") == "ESPERANDO_INFO_RECLAMO_LLM"

if __name__ == '__main__':
    unittest.main()
