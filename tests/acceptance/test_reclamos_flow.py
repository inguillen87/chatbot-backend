import pytest
from unittest.mock import patch
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio

@pytest.fixture(scope='module')
def test_client():
    app = create_app('config.TestingConfig')
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            rubro = Rubro(nombre="municipio", clave="municipio")
            db.session.add(rubro)
            owner_user = User(
                name="Test Municipio", email="test@municipio.com", password_hash="test",
                rubro=rubro, tipo_chat="municipio", municipio_id=1
            )
            db.session.add(owner_user)
            db.session.commit()
            yield client
            db.drop_all()

def test_reclamo_happy_path_with_location(test_client):
    owner_user = User.query.first()
    chat_session_id = "whatsapp_1_happypath"
    chat_db_context = ChatSessionContext(chat_session_id=chat_session_id, user_id=owner_user.id, anon_id="12345")
    db.session.add(chat_db_context)
    db.session.commit()

    with patch('services.flows.reclamos.flow.geo_reverse.reverse') as mock_reverse, \
         patch('services.flows.reclamos.flow.tickets_integration.create') as mock_create_ticket:

        mock_reverse.return_value = {"direccion": "Calle Falsa 123", "distrito": "Springfield"}
        mock_create_ticket.return_value = {"id": "M-99999"}

        # 1. User starts the flow
        response1 = responder_municipio("iniciar reclamo", owner_user, owner_user.rubro, chat_db_context=chat_db_context, anon_id="12345")
        assert "Por favor, elegí una categoría para tu reclamo:" in response1['message_body']

        # 2. User selects category
        response2 = responder_municipio("Luminaria", owner_user, owner_user.rubro, chat_db_context=chat_db_context, anon_id="12345")
        assert "¿Cuál es la ubicación del problema?" in response2['message_body']

        # 3. User sends location
        location_payload = {'type': 'location', 'lat': -32.89, 'lon': -68.84}
        response3 = responder_municipio(location_payload, owner_user, owner_user.rubro, chat_db_context=chat_db_context, anon_id="12345")
        assert "Ubicación recibida: Calle Falsa 123" in response3['message_body']

        # 4. User provides description
        response4 = responder_municipio("El poste de luz está roto", owner_user, owner_user.rubro, chat_db_context=chat_db_context, anon_id="12345")
        assert "Por favor, confirmá los datos de tu reclamo:" in response4['message_body']
        assert "Categoría:** Luminaria" in response4['message_body']
        assert "Ubicación:** Calle Falsa 123" in response4['message_body']

        # 5. User confirms
        response5 = responder_municipio("sí", owner_user, owner_user.rubro, chat_db_context=chat_db_context, anon_id="12345")
        assert "¡Tu reclamo fue generado con éxito!" in response5['message_body']
        assert "N° de Ticket: M-99999" in response5['message_body']

        mock_create_ticket.assert_called_once()
        ticket_data = mock_create_ticket.call_args[0][0]
        assert ticket_data['categoria'] == 'Luminaria'
        assert ticket_data['direccion'] == 'Calle Falsa 123'
        assert ticket_data['descripcion'] == 'El poste de luz está roto'
        assert ticket_data['nombre_vecino'] == 'Vecino/a'
        assert ticket_data['telefono_vecino'] == '12345'
