import pytest
from unittest.mock import patch
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio

@pytest.mark.legacy
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

@pytest.mark.legacy
@pytest.mark.skip(reason="Test is outdated and tests a deprecated flow.")
def test_reclamo_happy_path_with_location(test_client):
    owner_user = User.query.first()
    chat_session_id = "whatsapp_1_happypath"
    chat_db_context = ChatSessionContext(chat_session_id=chat_session_id, user_id=owner_user.id, anon_id="12345")
    db.session.add(chat_db_context)
    db.session.commit()

    with patch('services.municipio_responder.obtener_direccion_de_coordenadas') as mock_reverse, \
         patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_create_ticket:

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


from services.municipio_responder import ConversationState

@patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
@patch('services.herramientas_municipio.validar_y_formatear_direccion')
@patch('services.municipio_responder.llamar_gemini')
def test_llm_claim_flow_with_slot_gate(mock_llamar_gemini, mock_validar_direccion, mock_crear_ticket, test_client):
    """
    Tests the full LLM-driven claim flow, ensuring the Slot Gate works as expected.
    """
    owner_user = User.query.filter_by(email="test@municipio.com").first()
    chat_session_id = "whatsapp_1_llm_gate_flow"
    chat_db_context = ChatSessionContext(chat_session_id=chat_session_id, user_id=owner_user.id, anon_id="gate_test")
    db.session.add(chat_db_context)
    db.session.commit()

    # --- Turn 1: User initiates claim, LLM asks for location ---
    mock_llamar_gemini.return_value = (
        {
            "message_body": "Entendido, para registrar tu reclamo de luminaria, ¿podrías indicarme la dirección exacta?",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {"categoria": "Luminaria"},
            "pedir_info": "ubicacion"
        },
        {}, {} # context and usage_metadata
    )

    response1 = responder_municipio(
        "Quiero hacer un reclamo por una luz rota",
        owner_user, owner_user.rubro, chat_db_context=chat_db_context, anon_id="gate_test"
    )

    assert "para registrar tu reclamo de luminaria, ¿podrías indicarme la dirección exacta?" in response1['message_body']
    mock_llamar_gemini.assert_called_once()
    context_after_turn1 = ChatSessionContext.query.get(chat_session_id).context_data['contexto_municipio_v2']
    assert context_after_turn1['estado_conversacion'] == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
    assert context_after_turn1['esperando_info_llm_reclamo'] == 'ubicacion'
    assert context_after_turn1['datos_parciales_llm_reclamo']['categoria'] == 'Luminaria'

    # --- Turn 2: User provides location, Slot Gate asks for description ---
    mock_validar_direccion.return_value = {
        "formatted_address": "Avenida Siempreviva 742, Springfield",
        "latitude": -32.9, "longitude": -68.9
    }
    mock_llamar_gemini.reset_mock()

    viewer_user = User(
        id=99, name="Bart Simpson", telefono="5551234", email="bart@test.com", password_hash="test"
    )
    db.session.add(viewer_user)
    chat_db_context.user_id = viewer_user.id
    db.session.commit()

    response2 = responder_municipio(
        "Avenida Siempreviva 742",
        owner_user, owner_user.rubro, chat_db_context=chat_db_context, viewer_user=viewer_user, anon_id="gate_test"
    )

    mock_llamar_gemini.assert_not_called()
    mock_validar_direccion.assert_called_once_with("Avenida Siempreviva 742")
    assert "necesito algunos datos más: **descripcion**" in response2['message_to_user']
    context_after_turn2 = ChatSessionContext.query.get(chat_session_id).context_data['contexto_municipio_v2']
    assert context_after_turn2['datos_parciales_llm_reclamo']['ubicacion']['formatted_address'] == "Avenida Siempreviva 742, Springfield"

    # --- Turn 3: User provides description, Handler asks for confirmation ---
    viewer_user = User(
        id=99, name="Bart Simpson", telefono="5551234", email="bart@test.com"
    )
    db.session.add(viewer_user)
    chat_db_context.user_id = viewer_user.id
    db.session.commit()

    response3 = responder_municipio(
        "El poste de luz no enciende.",
        owner_user, owner_user.rubro, chat_db_context=chat_db_context, viewer_user=viewer_user, anon_id="gate_test"
    )

    mock_llamar_gemini.assert_not_called()
    assert "Por favor, confirmá si los datos para tu reclamo son correctos" in response3['message_to_user']
    assert "Luminaria" in response3['message_to_user']
    assert "Avenida Siempreviva 742, Springfield" in response3['message_to_user']
    assert "El poste de luz no enciende" in response3['message_to_user']
    assert "Bart Simpson" in response3['message_to_user']

    context_after_turn3 = ChatSessionContext.query.get(chat_session_id).context_data['contexto_municipio_v2']
    assert context_after_turn3['estado_conversacion'] == 'ESPERANDO_CONFIRMACION_DATOS_RECLAMO'
    assert 'datos_a_confirmar' in context_after_turn3

    # --- Turn 4: User confirms, ticket is created ---
    from models import MunicipioTicket
    mock_crear_ticket.return_value = MunicipioTicket(id=555, nro_ticket=12345)

    response4 = responder_municipio(
        "Sí, confirmo",
        owner_user, owner_user.rubro, chat_db_context=chat_db_context, viewer_user=viewer_user, anon_id="gate_test"
    )

    mock_llamar_gemini.assert_not_called()
    mock_crear_ticket.assert_called_once()

    # Extract ticket_data from the call arguments of the mock
    # The handler is called with create_ticket_now=True, and the data is in the first positional arg.
    call_args, call_kwargs = mock_crear_ticket.call_args
    ticket_data = call_kwargs['ticket_data']

    assert ticket_data['categoria'] == 'Luminaria'
    assert ticket_data['detalles'] == 'El poste de luz no enciende.'
    assert ticket_data['direccion'] == "Avenida Siempreviva 742, Springfield"
    assert ticket_data['nombre_vecino'] == 'Bart Simpson'

    assert "Tu reclamo fue generado con éxito" in response4['message_to_user']
    assert "M-12345" in response4['message_to_user']
