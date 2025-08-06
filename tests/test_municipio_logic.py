import pytest
from unittest.mock import patch, MagicMock
from models import User, Rubro, ChatSessionContext
from app import db
from config import TestingConfig
from services import municipios
from types import SimpleNamespace
import uuid
import json

# Fixture to create a basic user and rubro for tests
@pytest.fixture
def setup_test_environment(client):
    with client.application.app_context():
        # Using a consistent user for owner
        owner_user = User(id=1, email='owner@test.com', name='Municipio Owner', nombre_empresa='Municipio Test', rubro_id=1, tipo_chat='municipio')
        owner_user.set_password('ownerpass')
        # A separate, consistent user for viewer/citizen
        viewer_user = User(id=200, email='viewer@test.com', name='Viewer User')
        viewer_user.set_password('viewerpass')

        rubro = Rubro(id=1, nombre='municipio', clave='municipio')

        # Create a real ChatSessionContext for the tests
        session_id = str(uuid.uuid4())
        chat_context = ChatSessionContext(chat_session_id=session_id, user_id=viewer_user.id, context_data={})

        db.session.add(owner_user)
        db.session.add(viewer_user)
        db.session.add(rubro)
        db.session.add(chat_context)
        db.session.commit()

        yield owner_user, viewer_user, rubro, chat_context

def test_greeting_variation(client, setup_test_environment):
    """
    Tests that a greeting message triggers the welcome menu.
    """
    owner_user, viewer_user, rubro, chat_context = setup_test_environment

    with patch('services.municipios.GreetingHandler.handle') as mock_greeting_handler:
        mock_greeting_handler.return_value = {
            "respuesta_usuario": "¡Hola! Soy JUNI, el asistente virtual de la Municipalidad de Junín.",
            "botones": [{"texto": "Hacer un reclamo", "action_id": "iniciar_reclamo"}]
        }

        with client.application.app_context():
            resp = municipios.responder_municipio(
                pregunta_original={'pregunta': 'hola buenos noches'},
                owner_user=owner_user,
                rubro_obj=rubro,
                viewer_user=viewer_user,
                chat_db_context=chat_context
            )

    assert "¡Hola! Soy JUNI" in resp['respuesta_usuario']
    assert len(resp['botones']) > 0
    assert resp['botones'][0]['action_id'] == 'iniciar_reclamo'

def test_small_talk_municipio(client, setup_test_environment, mocker):
    """
    Tests that a small talk message gets a generic LLM response.
    """
    owner_user, viewer_user, rubro, chat_context = setup_test_environment

    # Configure the mock response for this specific test
    mock_response_json = json.dumps({
        "respuesta_usuario": "Todo bien por aquí, listo para ayudarte.",
        "accion_backend": "responder_directamente",
        "datos_estructura": {}, "pedir_info": None, "botones": []
    })

    # Create the nested mock structure that gemini_bridge expects
    mock_part = MagicMock()
    mock_part.text = mock_response_json
    mock_content = MagicMock()
    mock_content.parts = [mock_part]
    mock_candidate = MagicMock()
    mock_candidate.content = mock_content
    mock_gemini_response = MagicMock()
    mock_gemini_response.candidates = [mock_candidate]

    mocker.patch(
        'vertexai.generative_models.GenerativeModel.generate_content',
        return_value=mock_gemini_response
    )

    with client.application.app_context():
        resp = municipios.responder_municipio(
            pregunta_original={'pregunta': '¿Cómo te va?'},
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )

    assert "Todo bien por aquí" in resp['respuesta_usuario']
    assert resp['accion_backend'] == 'responder_directamente'

def test_tramite_selection_flow(client, setup_test_environment, mocker):
    """
    Tests the flow of asking for a "trámite" and getting information.
    """
    owner_user, viewer_user, rubro, chat_context = setup_test_environment

    # --- Step 1 ---
    mock_response_json_step1 = json.dumps({
        "respuesta_usuario": "¿Sobre qué trámite necesitas información?",
        "accion_backend": "consultar_tramite",
        "datos_estructura": {},
        "pedir_info": "nombre_tramite",
        "botones": [{"texto": "Licencia de Conducir"}, {"texto": "Rentas"}]
    })
    mock_part_step1 = MagicMock()
    mock_part_step1.text = mock_response_json_step1
    mock_gemini_response_step1 = MagicMock()
    mock_gemini_response_step1.candidates = [MagicMock(content=MagicMock(parts=[mock_part_step1]))]

    mocker.patch(
        'vertexai.generative_models.GenerativeModel.generate_content',
        return_value=mock_gemini_response_step1
    )

    with client.application.app_context():
        resp1 = municipios.responder_municipio(
            pregunta_original={'pregunta': 'Quiero hacer un tramite'},
            owner_user=owner_user,
            rubro_obj=rubro,
            viewer_user=viewer_user,
            chat_db_context=chat_context
        )

    assert "¿Sobre qué trámite necesitas información?" in resp1['respuesta_usuario']
    db.session.refresh(chat_context)
    assert chat_context.context_data['municipio_context_v2']['estado_conversacion'] == 'ESPERANDO_INFO_TRAMITE'

    # --- Step 2 ---
    # No need to mock gemini here, as we are mocking the action handler directly
    with patch('services.municipios.ConsultarInfoTramiteActionHandler.handle') as mock_info_handler:
        mock_info_handler.return_value = {
            "respuesta_usuario": "Para la Licencia de Conducir, visita nuestro sitio web.",
            "botones": [{"texto": "Ir al sitio", "url": "http://example.com/licencia"}]
        }

        with client.application.app_context():
            resp2 = municipios.responder_municipio(
                pregunta_original={'pregunta': 'Licencia de Conducir'},
                owner_user=owner_user,
                rubro_obj=rubro,
                viewer_user=viewer_user,
                chat_db_context=chat_context
            )

    mock_info_handler.assert_called_once()
    assert "Para la Licencia de Conducir" in resp2['respuesta_usuario']
    assert resp2['botones'][0]['url'] == "http://example.com/licencia"
