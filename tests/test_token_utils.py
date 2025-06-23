import unittest
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
