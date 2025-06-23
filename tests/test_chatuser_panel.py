import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import create_app

class DummyQuery:
    def __init__(self, result=None):
        self._result = result
    def filter_by(self, **kwargs):
        return DummyQuery(self._result)
    def first(self):
        return self._result

class ChatUserPanelTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_register_requires_token(self):
        resp = self.client.post('/chatuserregisterpanel', json={
            'name': 'Ana', 'email': 'ana@example.com', 'password': '123'}
        )
        self.assertEqual(resp.status_code, 400)

    def test_login_invalid_token(self):
        UserMock = SimpleNamespace(query=SimpleNamespace(filter_by=lambda **k: DummyQuery(None)))
        with patch('routes.auth.User', UserMock):
            resp = self.client.post('/chatuserloginpanel', json={
                'empresa_token': 'bad', 'email': 'a@b.com', 'password': '123'}
            )
            self.assertEqual(resp.status_code, 404)

if __name__ == '__main__':
    unittest.main()
