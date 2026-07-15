import pytest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestingConfig
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio

@pytest.fixture(scope='module')
def test_client():
    """Configura la aplicación Flask para las pruebas."""
    app = create_app(TestingConfig)
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
        "TWILIO_ACCOUNT_SID": "test_sid",
        "TWILIO_AUTH_TOKEN": "test_token",
    })

    with app.app_context():
        db.create_all()
        db.session.query(ChatSessionContext).delete()
        db.session.query(User).delete()
        db.session.query(Rubro).delete()
        db.session.commit()

        rubro = Rubro(nombre="municipio", clave="municipio")
        db.session.add(rubro)
        user = User(
            name="Test User",
            email="test@example.com",
            password_hash="test",
            nombre_empresa="Municipalidad de Test",
            tipo_chat="municipio",
            rubro=rubro,
            municipio_id=1
        )
        db.session.add(user)
        db.session.commit()

    with app.test_client() as testing_client:
        with app.app_context():
            yield testing_client
    with app.app_context():
        db.drop_all()

@pytest.fixture
def mock_llm():
    """Mock para la función llamar_gemini."""
    with patch('services.municipio_responder.llamar_gemini') as mock:
        yield mock

def test_full_claim_in_one_go(test_client, mock_llm):
    """Prueba la creación de un reclamo cuando el usuario da toda la info de una vez."""
    owner_user = User.query.first()
    chat_session_id = "whatsapp_1_123456789"
    chat_db_context = ChatSessionContext(chat_session_id=chat_session_id, user_id=owner_user.id, anon_id="123456789")
    db.session.add(chat_db_context)
    db.session.commit()

    mock_llm.return_value = (
        {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Semáforos",
                "descripcion": "El semáforo de la esquina no funciona.",
                "ubicacion": "Av. Siempre Viva 123",
                "usuario": "Marcelo Guillen",
                "telefono": "2613168608",
                "email": "marcelo.guillen@example.com",
                "dni": "32877851"
            },
            "message_body": "Gracias, he registrado tu reclamo."
        },
        {}
    )

    pregunta = "Quiero reportar un semáforo roto en Av. Siempre Viva 123. Mi nombre es Marcelo Guillen, mi teléfono es 2613168608 y mi email es marcelo.guillen@example.com."

    with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
        mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "12345"}

        respuesta = responder_municipio(
            pregunta_original=pregunta,
            owner_user=owner_user,
            rubro_obj=owner_user.rubro,
            chat_db_context=chat_db_context,
            anon_id="123456789"
        )

        assert "M-12345" in respuesta["message_to_user"]
        assert "Reclamo recibido" in respuesta["message_to_user"]
        mock_crear_ticket.assert_called_once()
        args, kwargs = mock_crear_ticket.call_args
        assert kwargs['ticket_data']['categoria'] == "Semáforos"
        assert kwargs['ticket_data']['nombre_vecino'] == "Marcelo Guillen"

def test_claim_in_multiple_steps(test_client, mock_llm):
    """Prueba la creación de un reclamo en múltiples interacciones."""
    owner_user = User.query.first()
    chat_session_id = "whatsapp_1_987654321"
    chat_db_context = ChatSessionContext(chat_session_id=chat_session_id, user_id=owner_user.id, anon_id="987654321")
    db.session.add(chat_db_context)
    db.session.commit()

    mock_llm.return_value = (
        {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio", "descripcion": "semáforo roto", "categoria": "Semáforos"},
            "message_body": "Entendido, ¿dónde es el problema?",
            "pedir_info": "ubicacion"
        },
        {}
    )
    respuesta = responder_municipio(
        pregunta_original="semáforo roto",
        owner_user=owner_user,
        rubro_obj=owner_user.rubro,
        chat_db_context=chat_db_context,
        anon_id="987654321"
    )
    assert "¿dónde es el problema?" in respuesta["message_body"]

    mock_llm.return_value = (
        {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"target": "municipio", "ubicacion": "Calle Falsa 123"},
            "message_body": "Perfecto. ¿Tu nombre?",
            "pedir_info": "nombre_completo"
        },
        {}
    )
    respuesta = responder_municipio(
        pregunta_original="Calle Falsa 123",
        owner_user=owner_user,
        rubro_obj=owner_user.rubro,
        chat_db_context=chat_db_context,
        anon_id="987654321"
    )
    assert "datos más" in respuesta["message_body"]
    assert "email" in respuesta["message_body"]
    assert "nombre" in respuesta["message_body"]
    assert "telefono" in respuesta["message_body"]

    mock_llm.return_value = (
        {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "usuario": "Lisa Simpson",
                "telefono": "+5492615551234",
                "email": "lisa.simpson@example.com",
                "dni": "30111222"
            },
            "message_body": "Gracias, he registrado tu reclamo."
        },
        {}
    )

    with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
        mock_crear_ticket.return_value = {"id": 2, "nro_ticket": "54321"}

        respuesta = responder_municipio(
            pregunta_original="Lisa Simpson, +54 9 261 555-1234, lisa.simpson@example.com",
            owner_user=owner_user,
            rubro_obj=owner_user.rubro,
            chat_db_context=chat_db_context,
            anon_id="987654321"
        )

        assert "M-54321" in respuesta["message_to_user"]
        assert "Reclamo recibido" in respuesta["message_to_user"]
        mock_crear_ticket.assert_called_once()
        args, kwargs = mock_crear_ticket.call_args
        assert kwargs['ticket_data']['detalles'] == "semáforo roto"
        assert kwargs['ticket_data']['direccion'] == "Calle Falsa 123"
        assert kwargs['ticket_data']['nombre_vecino'] == "Lisa Simpson"
