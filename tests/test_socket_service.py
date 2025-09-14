import unittest
import pytest
from unittest.mock import patch
from app import create_app, db
from config import TestConfig
from socket_service import socketio
from models import User, Rubro

class TestSocketService(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        # Need to create a default user for the welcome message to work
        rubro = Rubro(id=1, clave='municipios', nombre='municipios')
        owner_user = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', rubro=rubro)
        owner_user.set_password('password')
        db.session.add(owner_user)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('socket_service.emit')
    @patch('services.municipio_responder.responder_municipio')
    @patch('socket_service.generar_audio')
    def test_audio_welcome_message(self, mock_generar_audio, mock_responder_municipio, mock_emit):
        # Arrange
        mock_responder_municipio.return_value = {
            "message_body": "¡Hola! Bienvenido.",
            "options_list": [],
            "generar_audio": True
        }
        mock_generar_audio.return_value = "http://example.com/audio.mp3"

        from socket_service import send_welcome_message

        # Act
        send_welcome_message(sid='test-sid', auth={'channel': 'web'})

        # Assert
        mock_responder_municipio.assert_called_once()
        mock_generar_audio.assert_called_once_with(text="¡Hola! Bienvenido.")

        self.assertEqual(mock_emit.call_count, 1)
        args, kwargs = mock_emit.call_args

        self.assertEqual(args[0], 'message') # Event name
        response_data = args[1]
        self.assertEqual(response_data['message_body'], "¡Hola! Bienvenido.")
        self.assertEqual(response_data['audio_url'], "http://example.com/audio.mp3")
        self.assertEqual(kwargs['room'], 'test-sid')
