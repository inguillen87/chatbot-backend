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
def test_greeting_handler(init_database, owner_user, viewer_user, rubro, mock_tts):
    # Mockear el manejador de saludos para que no dependa de la configuración de Junín
    with patch('services.municipios.GreetingHandler') as mock_greeting_handler:
        mock_handler_instance = mock_greeting_handler.return_value
        mock_handler_instance.handle.return_value = {
            "message_body": "Hola, bienvenido al test.",
            "fuente": "mocked_greeting"
        }

        # Simular una solicitud con un input de saludo
        response = responder_municipio(
            pregunta_original="hola",
            owner_user=owner_user,
            viewer_user=viewer_user,
            chat_db_context=MagicMock(),
            rubro_obj=rubro
        )

        # Verificar que el manejador de saludos fue llamado
        mock_greeting_handler.assert_called_once()
        mock_handler_instance.handle.assert_called_once()

        # Verificar que la respuesta es la que esperábamos del mock
        assert response["message_body"] == "Hola, bienvenido al test."
        assert response["fuente"] == "mocked_greeting"


@pytest.mark.legacy
def test_reclamo_handler_inicio(init_database, owner_user, viewer_user, rubro, mock_tts):
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "Entendido, iniciando reclamo. ¿Sobre qué es?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "descripcion"
        }

        # Simular una solicitud para iniciar un reclamo
        response = responder_municipio(
            pregunta_original="reclamo",
            owner_user=owner_user,
            viewer_user=viewer_user,
            chat_db_context=MagicMock(context_data={}),
            rubro_obj=rubro
        )

        mock_llamar_gemini.assert_called_once()
        assert response["message_body"] == "Entendido, iniciando reclamo. ¿Sobre qué es?"

@pytest.mark.legacy
@patch('services.llm_utils.llamar_llm_para_json_estructurado')
def test_responder_municipio_imagen(mock_llamar_gemini, init_database, owner_user, viewer_user, rubro, mock_tts):
    mock_llamar_gemini.return_value = {
        "message_body": "Gracias por la imagen. Parece un reclamo sobre Bacheo. ¿Es correcto?",
        "accion_backend": "confirmar_reclamo_auto",
        "datos_estructura": {"categoria": "Bacheo", "descripcion": "Parece ser un bache."}
    }

    datos_interpretados = {
        "es_reclamo": True,
        "categoria_sugerida": "Bacheo",
        "descripcion_sugerida": "Parece ser un bache."
    }

    response = responder_municipio(
        pregunta_original="Mira esta foto",
        owner_user=owner_user,
        viewer_user=viewer_user,
        chat_db_context=MagicMock(context_data={}),
        rubro_obj=rubro,
        datos_interpretados_archivo=datos_interpretados
    )

    assert "Gracias por la imagen" in response["message_body"]
    assert "Bacheo" in response["message_body"]

@pytest.mark.legacy
def test_button_click_sets_category_and_advances_flow(init_database, owner_user, viewer_user, rubro, mock_tts):
    """
    Tests that clicking a sub-category button correctly sets the category
    in the context and advances the conversation to the next step.
    """
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llamar_gemini:
        mock_llamar_gemini.return_value = {
            "message_body": "Entendido. Para el reclamo de Luminaria, por favor decime la descripción del problema y la dirección.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "descripcion_y_ubicacion"
        }

        chat_db_context = MagicMock(context_data={})

        # Simulate the user clicking the "Luminaria" button
        response = responder_municipio(
            pregunta_original="reclamoluminaria",
            owner_user=owner_user,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            rubro_obj=rubro,
            channel="web",
            action="reclamoluminaria" # This is what the frontend sends
        )

        # 1. Assert the bot's response asks for the next piece of info
        assert "decime la descripción del problema y la dirección" in response["message_body"]

        # 2. Assert that the context was updated correctly
        contexto_guardado = chat_db_context.context_data.get("contexto_municipio_v2", {})
        datos_reclamo = contexto_guardado.get("datos_parciales_llm_reclamo", {})

        assert datos_reclamo.get("categoria") == "Luminaria"
        assert contexto_guardado.get("estado_conversacion") == "ESPERANDO_INFO_RECLAMO_LLM"
        assert contexto_guardado.get("esperando_info_llm_reclamo") == "descripcion_y_ubicacion"

if __name__ == '__main__':
    unittest.main()
