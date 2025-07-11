import unittest
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_token_utils = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_token_utils not in sys.path:
    sys.path.insert(0, project_root_token_utils)

from app import create_app
from routes.auth import obtener_token

class TokenExtractionTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config['TESTING'] = True
        self.app = app

    def test_authorization_without_bearer(self):
        with self.app.test_request_context('/', headers={'Authorization': 'abc123'}):
            self.assertEqual(obtener_token(), 'abc123')

    def test_authorization_with_bearer(self):
        with self.app.test_request_context('/', headers={'Authorization': 'Bearer abc123'}):
            self.assertEqual(obtener_token(), 'abc123')

if __name__ == '__main__':
    unittest.main()
