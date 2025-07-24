import pytest
from unittest.mock import patch, MagicMock
from services.logic import responder_chatboc
from services.pymes import responder_pyme
from services.municipios import responder_municipio
from models import User, Rubro, ChatSessionContext

@pytest.fixture
def mock_db_session():
    with patch('services.logic.db.session') as mock_session:
        yield mock_session


@pytest.fixture
def mock_google_vision():
    with patch('services.interpretacion_imagen_service.analyze_image_from_content') as mock_vision:
        yield mock_vision

@pytest.fixture
def mock_document_ai():
    with patch('services.document_processing_service.document_processing_service.process_document') as mock_docai:
        yield mock_docai

@patch('services.pymes.ChatOrchestrator')
def test_responder_chatboc_pyme_flow(mock_orchestrator, mock_db_session, mock_llamar_gemini_pymes_session):
    # Mock de la respuesta de Gemini para una intención de PYME
    mock_llamar_gemini_pymes_session.return_value = {
        "respuesta_usuario": "Claro, ¿qué te gustaría pedir?",
        "accion_backend": "iniciar_pedido",
        "datos_estructura": {"target": "pyme"},
        "pedir_info": None,
        "botones": []
    }

    # Mock de la respuesta del orchestrator
    mock_orchestrator.return_value.execute_action.return_value = {
        "success": True,
        "message_to_user": "Claro, ¿qué te gustaría pedir?",
        "fuente": "pyme_iniciar_pedido_v2"
    }

    owner_user = User(id=1, nombre_empresa="Pyme Test", tipo_chat="pyme")
    viewer_user = User(id=2, name="Cliente Test")
    rubro = Rubro(id=1, nombre="retail")
    owner_user.rubro = rubro
    chat_context = ChatSessionContext(context_data={})

    response = responder_chatboc(
        pregunta="Quiero hacer un pedido",
        owner_user=owner_user,
        current_user=viewer_user,
        rubro_obj=rubro,
        chat_db_context=chat_context
    )

    assert response is not None
    assert "pyme_iniciar_pedido_v2" in response.get("fuente", "")
    assert "Claro, ¿qué te gustaría pedir?" in response.get("message_body", "")

@patch('services.municipios.ChatOrchestrator')
def test_responder_chatboc_municipio_flow(mock_orchestrator, mock_db_session, mock_llamar_gemini_municipios_session):
    # Mock de la respuesta de Gemini para una intención de Municipio
    mock_llamar_gemini_municipios_session.return_value = {
        "respuesta_usuario": "Entendido, iniciando un reclamo. ¿Cuál es la dirección del problema?",
        "accion_backend": "crear_reclamo",
        "datos_estructura": {"target": "municipio"},
        "pedir_info": "ubicacion",
        "botones": []
    }

    # Mock de la respuesta del orchestrator
    mock_orchestrator.return_value.execute_action.return_value = {
        "success": True,
        "message_to_user": "Entendido, iniciando un reclamo. ¿Cuál es la dirección del problema?",
        "fuente": "municipio_crear_reclamo_v2"
    }

    owner_user = User(id=3, nombre_empresa="Municipio Test", tipo_chat="municipio")
    viewer_user = User(id=4, name="Vecino Test")
    rubro = Rubro(id=2, nombre="municipio", es_publico=True)
    owner_user.rubro = rubro
    chat_context = ChatSessionContext(context_data={})

    response = responder_chatboc(
        pregunta="Hay un bache en mi calle",
        owner_user=owner_user,
        current_user=viewer_user,
        rubro_obj=rubro,
        chat_db_context=chat_context
    )

    assert response is not None
    assert "municipio_crear_reclamo_v2" in response.get("fuente", "")
    assert "¿Cuál es la dirección del problema?" in response.get("message_body", "")

@patch('services.municipios.ChatOrchestrator')
def test_image_analysis_reclamo_municipio(mock_orchestrator, mock_db_session, mock_google_vision, mock_llamar_gemini_municipios_session):
    # Mock de la respuesta de Google Vision
    mock_google_vision.return_value = {
        "labels": [{"description": "pothole", "confidence": 0.9}],
        "objects": [],
        "full_text_annotation": None
    }

    # Mock de la respuesta de Gemini
    mock_llamar_gemini_municipios_session.return_value = {
        "respuesta_usuario": "He recibido la imagen. Parece un problema de 'Arreglo de calle'. ¿Cuál es la dirección?",
        "accion_backend": "crear_reclamo",
        "datos_estructura": {"target": "municipio", "categoria_sugerida": "Arreglo de calle"},
        "pedir_info": "ubicacion",
        "botones": []
    }

    # Mock de la respuesta del orchestrator
    mock_orchestrator.return_value.execute_action.return_value = {
        "success": True,
        "message_to_user": "He recibido la imagen. Parece un problema de 'Arreglo de calle'. ¿Cuál es la dirección?",
        "fuente": "municipio_crear_reclamo_v2"
    }

    owner_user = User(id=3, nombre_empresa="Municipio Test", tipo_chat="municipio")
    viewer_user = User(id=4, name="Vecino Test")
    rubro = Rubro(id=2, nombre="municipio", es_publico=True)
    owner_user.rubro = rubro
    chat_context = ChatSessionContext(context_data={})

    response = responder_municipio(
        pregunta_original={"pregunta": "", "uploaded_file_info": {"url": "http://example.com/bache.jpg", "mime_type": "image/jpeg"}},
        owner_user=owner_user,
        viewer_user=viewer_user,
        rubro_obj=rubro,
        chat_db_context=chat_context
    )

    assert response is not None
    assert "Arreglo de calle" in response.get("message_body", "")
    assert "dirección" in response.get("message_body", "")

@patch('services.pymes.ChatOrchestrator')
def test_document_processing_pedido_pyme(mock_orchestrator, mock_db_session, mock_document_ai, mock_llamar_gemini_pymes_session):
    # Mock de la respuesta de Document AI
    mock_document_ai.return_value = MagicMock(text="2 Coca Cola\n1 Papas Fritas")

    # Mock de la respuesta de Gemini
    mock_llamar_gemini_pymes_session.return_value = {
        "respuesta_usuario": "He recibido el pedido. ¿Deseas confirmar?",
        "accion_backend": "iniciar_pedido",
        "datos_estructura": {"target": "pyme"},
        "pedir_info": None,
        "botones": []
    }

    # Mock de la respuesta del orchestrator
    mock_orchestrator.return_value.execute_action.return_value = {
        "success": True,
        "message_to_user": "He recibido el pedido. ¿Deseas confirmar?",
        "fuente": "pyme_iniciar_pedido_v2"
    }

    owner_user = User(id=1, nombre_empresa="Pyme Test", tipo_chat="pyme")
    viewer_user = User(id=2, name="Cliente Test")
    rubro = Rubro(id=1, nombre="retail")
    owner_user.rubro = rubro
    chat_context = ChatSessionContext(context_data={})

    response = responder_pyme(
        pregunta_original={"pregunta": "", "uploaded_file_info": {"url": "http://example.com/pedido.pdf", "mime_type": "application/pdf"}},
        owner_user=owner_user,
        viewer_user=viewer_user,
        rubro_obj=rubro,
        chat_db_context=chat_context
    )

    assert response is not None
    assert "confirmar" in response.get("message_body", "").lower()
