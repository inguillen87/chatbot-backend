import pytest
from unittest.mock import patch, MagicMock
from app import db
from models import ChatSessionContext
from services.municipio_responder import responder_municipio, ConversationState, CONTEXTO_MUNICIPIO

@pytest.mark.legacy
def test_claim_flow_step_by_step(init_database, owner_user, viewer_user, rubro):
    """
    Simulates a step-by-step claim creation process, verifying context and state transitions.
    """
    chat_session = ChatSessionContext(chat_session_id="claim_flow_test_1")
    db.session.add(chat_session)
    db.session.commit()

    # Step 1: User initiates a claim
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llm:
        mock_llm.return_value = {
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {"categoria": "Alumbrado"},
            "pedir_info": "ubicacion",
            "message_body": "Entendido, reclamo de Alumbrado. ¿Dónde es?"
        }
        response = responder_municipio(
            pregunta_original="Luz quemada",
            owner_user=owner_user,
            viewer_user=viewer_user,
            rubro_obj=rubro,
            chat_db_context=chat_session
        )
        assert "¿Dónde es?" in response['message_body']
        context = chat_session.context_data[CONTEXTO_MUNICIPIO]
        assert context['estado_conversacion'] == 'ESPERANDO_INFO_RECLAMO_LLM'
        assert context['datos_parciales_llm_reclamo']['categoria'] == 'Alumbrado'

    # Step 2: User provides location
    with patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llm:
        mock_llm.return_value = {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"ubicacion": "Calle Falsa 123"},
            "pedir_info": "nombre_completo",
            "message_body": "OK. ¿Tu nombre?"
        }
        response = responder_municipio(
            pregunta_original="Calle Falsa 123",
            owner_user=owner_user,
            viewer_user=viewer_user,
            rubro_obj=rubro,
            chat_db_context=chat_session
        )
        assert "¿Tu nombre?" in response['message_body']
        context = chat_session.context_data[CONTEXTO_MUNICIPIO]
        assert context['datos_parciales_llm_reclamo']['ubicacion'] == 'Calle Falsa 123'

    # Step 3: User provides name, ticket is created
    with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_create_ticket, \
            patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llm:

        mock_ticket = MagicMock()
        mock_ticket.nro_ticket = "T-54321"
        mock_create_ticket.return_value = mock_ticket

        mock_llm.return_value = {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"nombre_usuario_detectado": "Juan Perez"},
            "pedir_info": None,
            "message_body": "¡Gracias, Juan! Tu reclamo ha sido creado."
        }

        response = responder_municipio(
            pregunta_original="Juan Perez",
            owner_user=owner_user,
            viewer_user=viewer_user,
            rubro_obj=rubro,
            chat_db_context=chat_session
        )
        assert "T-54321" in response['message_body']
        mock_create_ticket.assert_called_once()
        context = chat_session.context_data.get(CONTEXTO_MUNICIPIO, {})
        assert 'datos_parciales_llm_reclamo' not in context # Context should be cleared

@pytest.mark.legacy
def test_claim_flow_all_info_at_once(init_database, owner_user, viewer_user, rubro):
    """
    Tests creating a claim when the user provides all information in a single message.
    """
    chat_session = ChatSessionContext(chat_session_id="claim_flow_test_2")
    db.session.add(chat_session)
    db.session.commit()

    with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_create_ticket, \
            patch('services.llm_utils.llamar_llm_para_json_estructurado') as mock_llm:

        mock_ticket = MagicMock()
        mock_ticket.nro_ticket = "T-12345"
        mock_create_ticket.return_value = mock_ticket

        mock_llm.return_value = {
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "categoria": "Bache",
                "ubicacion": "Av. Siempreviva 742",
                "descripcion": "Agujero gigante",
                "nombre_usuario_detectado": "Homero Simpson"
            },
            "pedir_info": None,
            "message_body": "Reclamo creado."
        }

        response = responder_municipio(
            pregunta_original="Quiero reportar un bache en Av. Siempreviva 742, es un agujero gigante. Soy Homero Simpson.",
            owner_user=owner_user,
            viewer_user=viewer_user,
            rubro_obj=rubro,
            chat_db_context=chat_session
        )
        assert "T-12345" in response['message_body']
        mock_create_ticket.assert_called_once()
        call_args = mock_create_ticket.call_args[1]
        assert call_args['ticket_data']['categoria'] == 'Bache'
        assert call_args['ticket_data']['nombre_vecino'] == 'Homero Simpson'
