import unittest
from unittest.mock import patch, MagicMock
from flask import Flask
from app import create_app, db
from models import User
from routes.chat import chat_bp

class AskPymeFixTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.chat._procesar_chat')
    @patch('routes.chat._log_widget_request')
    def test_ask_pyme_redirects_for_municipio_owner(self, mock_log, mock_procesar):
        """
        Verifies that asking /ask/pyme with a user that is a 'municipio'
        redirects the logic to 'municipio'.
        """
        # Create a municipality owner
        user = User(
            email='muni@example.com',
            name='Muni Owner',
            rol='admin',
            tipo_chat='municipio'
        )
        user.set_password('password')
        db.session.add(user)
        db.session.commit()

        # Generate token with all required args
        from utils.auth_helpers import generar_token
        # def generar_token(user_id, rol, tipo_chat, municipio_id=None, pyme_id=None, tenant_slug=None, empresa_id=None):
        # We need to check the actual signature of generar_token in utils/auth_helpers.py
        # Based on error, it takes positional args.
        token = generar_token(user.id, user.rol, user.tipo_chat, None, None)

        headers = {'Authorization': f'Bearer {token}'}

        mock_procesar.return_value = {"respuesta": "ok"}

        # Call /ask/pyme
        response = self.client.post(
            '/ask/pyme',
            json={'pregunta': 'hola'},
            headers=headers
        )

        self.assertEqual(response.status_code, 200)

        # Check that _procesar_chat was called with "municipio"
        args, kwargs = mock_procesar.call_args
        self.assertEqual(args[0], 'municipio', "Should redirect to 'municipio' logic")

    @patch('routes.chat._procesar_chat')
    @patch('routes.chat._log_widget_request')
    def test_ask_pyme_normal_behavior(self, mock_log, mock_procesar):
        """
        Verifies that asking /ask/pyme with a normal pyme user stays as 'pyme'.
        """
        user = User(
            email='pyme@example.com',
            name='Pyme Owner',
            rol='admin',
            tipo_chat='pyme'
        )
        user.set_password('password')
        db.session.add(user)
        db.session.commit()

        from utils.auth_helpers import generar_token
        token = generar_token(user.id, user.rol, user.tipo_chat, None, None)

        headers = {'Authorization': f'Bearer {token}'}
        mock_procesar.return_value = {"respuesta": "ok"}

        response = self.client.post(
            '/ask/pyme',
            json={'pregunta': 'hola'},
            headers=headers
        )

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_procesar.call_args
        self.assertEqual(args[0], 'pyme', "Should stay as 'pyme' logic")
