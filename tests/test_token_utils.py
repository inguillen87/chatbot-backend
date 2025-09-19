import unittest
from flask import Flask

from routes.auth import obtener_token

class TokenExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="testing-secret",
            AUTH_TOKEN_COOKIE_NAME="auth_token",
            WIDGET_TOKEN_COOKIE_NAME="widget_token",
        )

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

    def test_token_from_widget_cookie(self):
        widget_cookie = self.app.config.get('WIDGET_TOKEN_COOKIE_NAME', 'widget_token')
        headers = {'Cookie': f'{widget_cookie}=widget123'}
        with self.app.test_request_context('/', headers=headers):
            self.assertEqual(obtener_token(), 'widget123')

if __name__ == '__main__':
    unittest.main()
