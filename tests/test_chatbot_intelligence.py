import pytest
from unittest.mock import patch, MagicMock
from services.logic import responder_chatboc
from services.pymes import responder_pyme
from services.municipio_responder import responder_municipio
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

@patch('services.logic.responder_pyme')
def test_responder_chatboc_pyme_flow(mock_responder_pyme, mock_db_session):
    mock_responder_pyme.return_value = {
        "fuente": "pyme_iniciar_pedido_v2",
        "message_body": "¿Qué productos y cantidades te gustaría pedir? También puedes subir un archivo Excel."
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
    assert "¿Qué productos y cantidades te gustaría pedir? También puedes subir un archivo Excel." in response.get("message_body", "")

@patch('services.logic.responder_municipio')
def test_responder_chatboc_municipio_flow(mock_responder_municipio, mock_db_session):
    mock_responder_municipio.return_value = {
        "fuente": "municipio_crear_reclamo_v2",
        "message_body": "¿Cuál es la dirección del problema?"
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

@patch('services.municipio_responder.llamar_gemini')
@patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat')
def test_image_analysis_reclamo_municipio(mock_interpretar_imagen, mock_llamar_gemini, client, mock_db_session):
    mock_interpretar_imagen.return_value = {
        "es_reclamo": True,
        "categoria_sugerida": "Arreglo de calle",
        "descripcion_sugerida": "Bache en la calle"
    }
    mock_llamar_gemini.return_value = (
        {
            "message_body": "Gracias por la imagen. Parece un reclamo sobre 'Arreglo de calle'. Para continuar, por favor decime la dirección.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "categoria": "Arreglo de calle",
                "descripcion": "Bache en la calle"
            },
            "pedir_info": "ubicacion"
        },
        {}
    )

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

@pytest.mark.skip(reason="Document processing for pymes is being refactored.")
def test_document_processing_pedido_pyme(mock_db_session):
    pass

@patch('services.municipio_responder.llamar_gemini')
def test_information_gathering_reclamo_municipio(mock_llamar_gemini, client, mock_db_session):
    # 1. Initial request to create a reclamo
    mock_llamar_gemini.return_value = (
        {
            "message_body": "Entendido, iniciando un reclamo. ¿Cuál es la dirección del problema?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "ubicacion",
            "botones": []
        },
        {}
    )

    owner_user = User(id=3, nombre_empresa="Municipio Test", tipo_chat="municipio")
    viewer_user = User(id=4, name="Vecino Test")
    rubro = Rubro(id=2, nombre="municipio", es_publico=True)
    owner_user.rubro = rubro
    chat_context = ChatSessionContext(context_data={})

    response1 = responder_municipio(
        pregunta_original="Hay un bache en mi calle",
        owner_user=owner_user,
        viewer_user=viewer_user,
        rubro_obj=rubro,
        chat_db_context=chat_context
    )

    assert response1 is not None
    assert "dirección" in response1.get("message_body", "")
    assert chat_context.context_data['contexto_municipio_v2']['estado_conversacion'] == 'ESPERANDO_INFO_RECLAMO_LLM'

    # 2. User provides the location
    mock_llamar_gemini.return_value = (
        {
            "message_body": "Gracias. Ahora necesito tu nombre completo.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio", "ubicacion": "Calle Falsa 123"},
            "pedir_info": "nombre_completo",
            "botones": []
        },
        {}
    )

    response2 = responder_municipio(
        pregunta_original="Calle Falsa 123",
        owner_user=owner_user,
        viewer_user=viewer_user,
        rubro_obj=rubro,
        chat_db_context=chat_context
    )

    assert response2 is not None
    assert "nombre completo" in response2.get("message_body", "")
    assert chat_context.context_data['contexto_municipio_v2']['esperando_info_llm_reclamo'] == 'nombre_completo'
