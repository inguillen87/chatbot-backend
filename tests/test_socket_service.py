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

    @patch('services.municipio_responder.responder_municipio')
    @patch('socket_service.tts_service.synthesize_speech')
    def test_audio_welcome_message(self, mock_synthesize_speech, mock_responder_municipio):
        # Arrange
        mock_responder_municipio.return_value = {
            "message_body": "¡Hola! Bienvenido a nuestro servicio de atención al cliente.",
            "options_list": []
        }
        mock_synthesize_speech.return_value = "http://example.com/audio.mp3"

        socketio_client = socketio.test_client(self.app)
        socketio_client.get_received() # clear any previous messages

        # Act
        # Pass channel in the auth dictionary, which is how the on_connect handler receives it
        socketio_client.connect(auth={'channel': 'web'})
        received = socketio_client.get_received()

        # Assert
        # We expect exactly one message: the welcome message.
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['name'], 'message')
        data = received[0]['args'][0]
        self.assertEqual(data['message_body'], "¡Hola! Bienvenido a nuestro servicio de atención al cliente.")
        self.assertEqual(data['audio_url'], "http://example.com/audio.mp3")
        mock_synthesize_speech.assert_called_once_with(text="¡Hola! Bienvenido a nuestro servicio de atención al cliente.")
        mock_responder_municipio.assert_called_once()
