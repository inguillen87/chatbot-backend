import unittest
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import User, Categoria
from config import TestConfig


class CategoriasEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        admin = User(
            id=1,
            name='admin',
            email='admin@test.com',
            password_hash='x',
            rol='admin',
            municipio_id=1,
        )
        admin.set_password('secret')
        db.session.add(admin)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _get_token(self):
        resp = self.client.post('/auth/login', json={'email': 'admin@test.com', 'password': 'secret'})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['token']

    def test_bootstraps_and_returns_objects(self):
        token = self._get_token()
        headers = {'Authorization': f'Bearer {token}'}

        self.assertEqual(Categoria.query.count(), 0)

        resp = self.client.get('/categorias', headers=headers)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()

        categorias = payload.get('categorias', [])
        self.assertGreater(len(categorias), 0)
        self.assertTrue(all('id' in c and 'nombre' in c for c in categorias))

        # Ensure categories persisted for subsequent calls
        self.assertGreater(Categoria.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
