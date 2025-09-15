import unittest
import pytest
from unittest.mock import patch, MagicMock, ANY, call
from types import SimpleNamespace

from app import create_app, db
from models import User, Rubro, MunicipioTicket, PymeTicket
from config import TestConfig
from services.actions.municipio_actions import DerivarHumanoActionHandler as MunicipioDerivarHandler
from services.actions.pyme_actions import DerivarHumanoActionHandlerPyme as PymeDerivarHandler
from services.chat_orchestrator import ChatOrchestrator

class TestDerivarHumanoAction:

    @pytest.fixture(autouse=True)
    def setup(self, init_database):
        self.db = init_database

    @patch('services.actions.municipio_actions.socketio.emit')
    @patch('services.actions.municipio_actions.emit_ticket_update')
    def test_crea_ticket_municipio_con_db_y_socket(self, mock_emit_update, mock_socket_emit):
        """
        Tests that a live chat ticket is created for a municipality,
        persisted in the DB, and a socket event is emitted.
        """
        # Arrange
        owner_user = User(id=10, municipio_id=10, name="Municipio Test", email="municipio@test.com")
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

        # Check database
        ticket_id = result['data']['ticket_id']
        ticket_db = db.session.query(MunicipioTicket).get(ticket_id)
        assert ticket_db is not None
        assert ticket_db.estado == 'esperando_agente_en_vivo'
        assert ticket_db.comentarios.count() == 1
        assert ticket_db.comentarios.first().comentario == 'Necesito ayuda con algo.'

        # Check socket emission
        mock_socket_emit.assert_any_call('live_chat_request', ANY, room='municipio_10')
        mock_emit_update.assert_called_once()

    @patch('services.actions.pyme_actions.emit_ticket_update')
    def test_crea_ticket_pyme_con_db(self, mock_emit_update):
        """
        Tests that a live chat ticket is created for a Pyme and persisted in the DB.
        """
        # Arrange
        owner_user = User(id=20, pyme_id=20, name="Pyme Test", email="pyme@test.com")
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

        # Check database
        ticket_id = result['data']['ticket_id']
        ticket_db = db.session.query(PymeTicket).get(ticket_id)
        assert ticket_db is not None
        assert ticket_db.estado == 'esperando_agente_en_vivo'
        assert ticket_db.comentarios.count() == 1
        assert ticket_db.comentarios.first().comentario == 'Consulta de producto.'
        mock_emit_update.assert_called_once()

    @patch('services.actions.pyme_actions.emit_ticket_update')
    def test_orchestrator_routes_to_pyme_handler(self, mock_emit_update):
        # Arrange
        owner_user = User(id=20, pyme_id=20, name="Pyme Test", email="pyme@test.com")
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
    def test_orchestrator_routes_to_municipio_handler(self, mock_emit_update, mock_socket_emit):
        # Arrange
        owner_user = User(id=10, municipio_id=10, name="Municipio Test", email="municipio@test.com")
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
        mock_socket_emit.assert_any_call('live_chat_request', ANY, room='municipio_10')
        mock_emit_update.assert_called_once()
