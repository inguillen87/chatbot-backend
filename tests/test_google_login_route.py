import sys
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import create_app, db
from models import User, Rubro
from config import TestConfig
from routes.auth import google_login
from config import TestConfig

class GoogleLoginRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_status_falta_rubro(self):
        user = User(id=1, email='a@b.com', name='A', password_hash='test')
        with self.app.test_request_context(json={'id_token': 'tok'}):
            with patch('routes.auth.login_o_crear_usuario', return_value=user):
                resp = google_login()
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['status'], 'falta_rubro')
        self.assertEqual(data['email'], 'a@b.com')
        self.assertIn('token', data)
        self.assertIsInstance(data['token'], str)
        self.assertTrue(len(data['token']) > 20) # JWTs are long
        self.assertIn('entity_token', data)
        self.assertIn('auth_token', data)
        self.assertEqual(data['auth_token'], data['token'])

    def test_login_normal(self):
        rubro = Rubro(nombre='IT', clave='it')
        user = User(id=2, email='b@c.com', name='B', token='t2', rubro=rubro, rubro_id=5, rol='usuario', empresa_id=None, ticket_categorias='', password_hash='test')
        with self.app.test_request_context(json={'id_token': 'tok'}):
            with patch('routes.auth.login_o_crear_usuario', return_value=user), \
                 patch('routes.auth.es_rubro_publico', lambda r: False):
                resp = google_login()
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['id'], 2)
        self.assertNotIn('status', data)
        self.assertEqual(data['rubro'], 'IT')
        self.assertEqual(data['entity_token'], 't2')
        self.assertEqual(data['owner_token'], 't2')
        self.assertEqual(data['auth_token'], data['token'])

if __name__ == '__main__':
    unittest.main()
