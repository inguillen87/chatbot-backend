import unittest
import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import User
from config import TestConfig


class CategoriasRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()
        user = User(id=1, name='test', email='test@test.com', password_hash='test', rol='admin')
        user.set_password('test')
        db.session.add(user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _get_token(self):
        login_response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'test'})
        self.assertEqual(login_response.status_code, 200)
        return login_response.get_json()['token']

    def test_listar_categorias_con_busqueda(self):
        token = self._get_token()
        headers = {'Authorization': f'Bearer {token}'}
        response = self.client.get('/categorias', headers=headers, query_string={'q': 'obra'})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        categorias = data.get('categorias', [])
        self.assertTrue(any('Obras Privadas' in c for c in categorias))
        self.assertTrue(all('obra' in c.lower() for c in categorias))

    def test_listar_categorias_sin_busqueda(self):
        token = self._get_token()
        headers = {'Authorization': f'Bearer {token}'}
        response = self.client.get('/categorias', headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertGreater(len(data.get('categorias', [])), 0)


if __name__ == '__main__':
    unittest.main()
