import unittest
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_token_utils = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_token_utils not in sys.path:
    sys.path.insert(0, project_root_token_utils)

from app import create_app
from routes.auth import obtener_token
from config import TestConfig

class TokenExtractionTests(unittest.TestCase):
    def setUp(self):
        app = create_app(TestConfig)
        self.app = app

    def test_authorization_without_bearer(self):
        with self.app.test_request_context('/', headers={'Authorization': 'abc123'}):
            self.assertEqual(obtener_token(), 'abc123')

    def test_authorization_with_bearer(self):
        with self.app.test_request_context('/', headers={'Authorization': 'Bearer abc123'}):
            self.assertEqual(obtener_token(), 'abc123')

    def test_token_from_cookie(self):
        cookie_name = self.app.config.get('AUTH_TOKEN_COOKIE_NAME', 'auth_token')
        headers = {'Cookie': f'{cookie_name}=abc123'}
        with self.app.test_request_context('/', headers=headers):
            self.assertEqual(obtener_token(), 'abc123')

if __name__ == '__main__':
    unittest.main()
