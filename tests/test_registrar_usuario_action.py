import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.actions.general_actions import RegistrarUsuarioActionHandler

class RegistrarUsuarioActionTests(unittest.TestCase):
    def test_invalid_token(self):
        with patch('services.actions.general_actions.User') as UserMock:
            UserMock.query.filter_by.return_value.first.return_value = None
            handler = RegistrarUsuarioActionHandler({})
            data = {
                'name': 'Ana',
                'email': 'ana@example.com',
                'password': '123',
                'empresa_token': 'bad'
            }
            result = handler.execute(data)
            self.assertFalse(result['success'])

    def test_success(self):
        owner = SimpleNamespace(id=1, rubro='municipio', rubro_id=2, tipo_chat='municipio')
        created_user = SimpleNamespace(id=5, email='ana@example.com', token='tnew')

        query_by_token = SimpleNamespace(first=lambda: owner)
        query_filter = SimpleNamespace(first=lambda: None)

        with patch('services.actions.general_actions.User') as UserMock, \
             patch('services.actions.general_actions.db') as db_mock, \
             patch('services.actions.general_actions.validar_email', return_value=True):
            UserMock.query.filter_by.return_value = query_by_token
            UserMock.query.filter.return_value = query_filter
            UserMock.return_value = created_user
            db_mock.session.add = lambda x: None
            db_mock.session.commit = lambda: None

            handler = RegistrarUsuarioActionHandler({})
            data = {
                'name': 'Ana',
                'email': 'ana@example.com',
                'password': '123',
                'empresa_token': 'token'
            }
            result = handler.execute(data)
            self.assertTrue(result['success'])
            self.assertEqual(result['data']['user_id'], created_user.id)

if __name__ == '__main__':
    unittest.main()
