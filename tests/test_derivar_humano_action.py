import unittest
import pytest
from unittest.mock import patch, MagicMock, ANY, call
from types import SimpleNamespace

from app import create_app, db
from models import User, Rubro, MunicipioTicket, PymeTicket, ChatSessionContext, TicketComentario
from config import TestConfig
from services.actions.municipio_actions import DerivarHumanoActionHandler as MunicipioDerivarHandler
from services.actions.pyme_actions import DerivarHumanoActionHandlerPyme as PymeDerivarHandler
from services.chat_orchestrator import ChatOrchestrator
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO
from services.pymes import responder_pyme, CONTEXTO_PYME

@pytest.fixture
def app_context():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()

class TestDerivarHumanoAction:

    @patch('services.actions.municipio_actions.socketio.emit')
    @patch('services.actions.municipio_actions.emit_ticket_update')
    def test_crea_ticket_municipio_con_db_y_socket(self, mock_emit_update, mock_socket_emit, app_context):
        """
        Tests that a live chat ticket is created for a municipality,
        persisted in the DB, and a socket event is emitted.
        """
        # Arrange
        owner_user = User(id=1, municipio_id=1, name="Municipio Test", email="municipio@test.com")
        owner_user.set_password("test")
        viewer_user = User(id=5, name="Juan", telefono="123456789", email="juan@test.com")
        viewer_user.set_password("test")
        db.session.add_all([owner_user, viewer_user])
        db.session.commit()

        context = {
            'viewer_user_obj': viewer_user,
            'user_obj': owner_user,
            'cliente_id': 5,
            'anon_id': None,
            'target_entity_type': 'municipio',
            'pregunta_actual_usuario': 'Necesito ayuda con algo.'
        }
        handler = MunicipioDerivarHandler(context)

        # Act
        result = handler.execute({'motivo_derivacion': 'prueba de socket'})

        # Assert
        assert result['success']
        assert 'M-' in result['data']['chat_id']
        assert isinstance(result['message_to_user'], str)

        # Check database
        ticket_id = result['data']['ticket_id']
        ticket_db = db.session.query(MunicipioTicket).get(ticket_id)
        assert ticket_db is not None
        assert ticket_db.estado == 'esperando_agente_en_vivo'
        assert ticket_db.comentarios.count() == 1
        assert ticket_db.comentarios.first().comentario == 'Necesito ayuda con algo.'

        # Check socket emission
        mock_socket_emit.assert_any_call('live_chat_request', ANY, room='municipio_1')
        mock_emit_update.assert_called_once()

    @patch('services.actions.pyme_actions.emit_ticket_update')
    def test_crea_ticket_pyme_con_db(self, mock_emit_update, app_context):
        """
        Tests that a live chat ticket is created for a Pyme and persisted in the DB.
        """
        # Arrange
        owner_user = User(id=2, pyme_id=20, name="Pyme Test", email="pyme@test.com")
        owner_user.set_password("test")
        viewer_user = User(id=9, name="Ana", telefono="987654321", email="ana@test.com")
        viewer_user.set_password("test")
        db.session.add_all([owner_user, viewer_user])
        db.session.commit()

        context = {
            'viewer_user_obj': viewer_user,
            'user_obj': owner_user,
            'cliente_id': 9,
            'anon_id': None,
            'target_entity_type': 'pyme',
            'pregunta_actual_usuario': 'Consulta de producto.'
        }
        handler = PymeDerivarHandler(context)

        # Act
        result = handler.execute({'motivo_derivacion': 'test pyme socket'})

        # Assert
        assert result['success']
        assert 'P-' in result['data']['chat_id']
        assert isinstance(result['message_to_user'], str)

        # Check database
        ticket_id = result['data']['ticket_id']
        ticket_db = db.session.query(PymeTicket).get(ticket_id)
        assert ticket_db is not None
        assert ticket_db.estado == 'esperando_agente_en_vivo'
        assert ticket_db.comentarios.count() == 1
        assert ticket_db.comentarios.first().comentario == 'Consulta de producto.'
        assert ticket_db.telefono == viewer_user.telefono
        assert ticket_db.email == viewer_user.email
        mock_emit_update.assert_called_once()

    @patch('services.actions.pyme_actions.emit_ticket_update')
    def test_orchestrator_routes_to_pyme_handler(self, mock_emit_update, app_context):
        # Arrange
        owner_user = User(id=2, pyme_id=20, name="Pyme Test", email="pyme@test.com")
        owner_user.set_password("test")
        viewer_user = User(id=9, name="Ana", telefono="987654321", email="ana@test.com")
        viewer_user.set_password("test")
        db.session.add_all([owner_user, viewer_user])
        db.session.commit()

        context = {
            'viewer_user_obj': viewer_user,
            'user_obj': owner_user,
            'cliente_id': 9,
            'anon_id': None,
            'target_entity_type': 'pyme',
            'pregunta_actual_usuario': 'Quiero hablar con alguien.'
        }
        orchestrator = ChatOrchestrator(global_context=context)

        # Act
        result = orchestrator.execute_action({'accion_backend': 'derivar_humano', 'datos_estructura': {'motivo_derivacion': 'test orchestrator'}})

        # Assert
        assert result['executed_action_handler'] == 'DerivarHumanoActionHandlerPyme'
        assert result['success']
        assert 'P-' in result['data']['chat_id']
        mock_emit_update.assert_called_once()


    @patch('services.actions.municipio_actions.socketio.emit')
    @patch('services.actions.municipio_actions.emit_ticket_update')
    def test_orchestrator_routes_to_municipio_handler(self, mock_emit_update, mock_socket_emit, app_context):
        # Arrange
        owner_user = User(id=1, municipio_id=1, name="Municipio Test", email="municipio@test.com")
        owner_user.set_password("test")
        viewer_user = User(id=5, name="Juan", telefono="123456789", email="juan@test.com")
        viewer_user.set_password("test")
        db.session.add_all([owner_user, viewer_user])
        db.session.commit()

        context = {
            'viewer_user_obj': viewer_user,
            'user_obj': owner_user,
            'cliente_id': 5,
            'anon_id': None,
            'target_entity_type': 'municipio',
            'pregunta_actual_usuario': 'Necesito ayuda con la app.'
        }
        orchestrator = ChatOrchestrator(global_context=context)

        # Act
        result = orchestrator.execute_action({'accion_backend': 'derivar_humano', 'datos_estructura': {'motivo_derivacion': 'test orchestrator municipio'}})

        # Assert
        assert result['executed_action_handler'] == 'DerivarHumanoActionHandler'
        assert result['success']
        assert 'M-' in result['data']['chat_id']
        mock_socket_emit.assert_any_call('live_chat_request', ANY, room='municipio_1')
        mock_emit_update.assert_called_once()

    @patch('services.municipio_responder.emit_ticket_update')
    @patch('services.municipio_responder.emit_new_chat_message')
    def test_municipio_live_chat_forwarding(self, mock_emit_new_message, mock_emit_update, app_context):
        owner_user = User(id=7, municipio_id=7, name="Municipio Live", email="live@muni.test", tipo_chat='municipio', rol='admin')
        owner_user.set_password("test")
        rubro = Rubro(id=1, nombre="Municipio", clave="municipio", es_publico=True)
        viewer_user = User(id=2, name="Vecino", telefono="111", email="vecino@test.com")
        viewer_user.set_password("test")
        db.session.add_all([rubro, owner_user, viewer_user])
        owner_user.rubro = rubro
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=owner_user.municipio_id,
            user_id=viewer_user.id,
            pregunta="Inicial",
            asunto="Chat",
            categoria="Atención en Vivo",
            detalles="motivo",
            nro_ticket=123456,
            estado="esperando_agente_en_vivo",
            nombre_vecino=viewer_user.name,
            telefono_vecino=viewer_user.telefono,
            email_vecino=viewer_user.email,
        )
        db.session.add(ticket)
        db.session.commit()

        context_data = {
            CONTEXTO_MUNICIPIO: {
                "live_chat_autoderivado": True,
                "live_chat_ticket_id": ticket.id,
                "live_chat_tipo": "municipio",
            }
        }
        chat_ctx = ChatSessionContext(chat_session_id="ctx-muni", user_id=owner_user.id, context_data=context_data)
        db.session.add(chat_ctx)
        db.session.commit()

        def fake_crear_comentario(ticket_id, tipo_ticket, comentario_data):
            comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"),
                anon_id=comentario_data.get("anon_id"),
                es_admin=False,
                origen=comentario_data.get("origen"),
            )
            comentario.municipio_ticket = ticket
            db.session.add(comentario)
            return comentario

        with patch('services.municipio_responder.servicio_tickets.crear_comentario', side_effect=fake_crear_comentario), \
             patch('services.municipio_responder.serialize_ticket_to_json', return_value={'id': ticket.id}):
            response = responder_municipio(
                "Necesito hablar ya",
                owner_user,
                owner_user.rubro,
                viewer_user=viewer_user,
                chat_db_context=chat_ctx,
                anon_id="anon-vecino",
                channel="whatsapp",
            )

        db.session.commit()

        assert response["fuente"] == "live_chat_forward_municipio"
        assert "Tu mensaje fue enviado" in response["message_body"]
        assert response["data"]["ticket_id"] == ticket.id
        assert response["contexto_actualizado"][CONTEXTO_MUNICIPIO]["live_chat_estado"] == "esperando_agente_en_vivo"
        comentarios = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).all()
        assert len(comentarios) == 1
        assert comentarios[0].comentario.startswith("Necesito hablar")
        mock_emit_new_message.assert_called_once()
        mock_emit_update.assert_called_once()
        ctx_guardado = ChatSessionContext.query.get(chat_ctx.chat_session_id)
        assert ctx_guardado.context_data[CONTEXTO_MUNICIPIO]["live_chat_estado"] == "esperando_agente_en_vivo"

    @patch('services.pymes.emit_ticket_update')
    @patch('services.pymes.emit_new_chat_message')
    def test_pyme_live_chat_forwarding(self, mock_emit_new_message, mock_emit_update, app_context):
        rubro = Rubro(id=2, nombre="Comercio", clave="comercio", es_publico=False)
        owner_user = User(id=10, name="Pyme Live", email="pyme@test.com", tipo_chat='pyme', rol='admin', token='tok-live', rubro_id=rubro.id)
        owner_user.set_password("test")
        viewer_user = User(id=11, name="Cliente", telefono="999", email="cliente@test.com")
        viewer_user.set_password("test")
        db.session.add_all([rubro, owner_user, viewer_user])
        owner_user.rubro = rubro
        db.session.commit()

        ticket = PymeTicket(
            pregunta="Consulta",
            asunto="Chat",
            categoria="Atención en Vivo",
            user_id=viewer_user.id,
            estado="esperando_agente_en_vivo",
            anon_id=None,
            nro_ticket=654321,
            telefono=viewer_user.telefono,
            email=viewer_user.email,
        )
        db.session.add(ticket)
        db.session.commit()

        context_data = {
            CONTEXTO_PYME: {
                "live_chat_autoderivado": True,
                "live_chat_ticket_id": ticket.id,
                "live_chat_tipo": "pyme",
            }
        }
        chat_ctx = ChatSessionContext(chat_session_id="ctx-pyme", user_id=owner_user.id, context_data=context_data)
        db.session.add(chat_ctx)
        db.session.commit()

        def fake_crear_comentario(ticket_id, tipo_ticket, comentario_data):
            comentario = TicketComentario(
                comentario=comentario_data.get("comentario"),
                user_id=comentario_data.get("user_id"),
                anon_id=comentario_data.get("anon_id"),
                es_admin=False,
                origen=comentario_data.get("origen"),
            )
            comentario.pyme_ticket = ticket
            db.session.add(comentario)
            return comentario

        with patch('services.pymes.servicio_tickets.crear_comentario', side_effect=fake_crear_comentario), \
             patch('services.pymes.serialize_ticket_to_json', return_value={'id': ticket.id}):
            response = responder_pyme(
                "¿Hola hay alguien?",
                owner_user,
                owner_user.rubro,
                viewer_user=viewer_user,
                chat_db_context=chat_ctx,
                anon_id="anon-cliente",
                channel="whatsapp",
            )

        db.session.commit()

        assert response["fuente"] == "live_chat_forward_pyme"
        assert "Tu mensaje fue enviado" in response["message_body"]
        assert response["data"]["ticket_id"] == ticket.id
        assert response["contexto_actualizado"][CONTEXTO_PYME]["live_chat_estado"] == "esperando_agente_en_vivo"
        comentarios = TicketComentario.query.filter_by(pyme_ticket_id=ticket.id).all()
        assert len(comentarios) == 1
        assert comentarios[0].comentario.startswith("¿Hola hay alguien")
        mock_emit_new_message.assert_called_once()
        mock_emit_update.assert_called_once()
        ctx_guardado = ChatSessionContext.query.get(chat_ctx.chat_session_id)
        assert ctx_guardado.context_data[CONTEXTO_PYME]["live_chat_estado"] == "esperando_agente_en_vivo"
