import sys
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

project_root_google_login = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_google_login not in sys.path:
    sys.path.insert(0, project_root_google_login)

try:
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class GoogleLoginRouteTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_status_falta_rubro(self):
        user = SimpleNamespace(id=1, email='a@b.com', name='A', token='t1', rubro=None, rubro_id=None)
        with patch('routes.auth.login_o_crear_usuario', return_value=user):
            resp = self.client.post('/google-login', json={'id_token': 'tok'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            'status': 'falta_rubro',
            'token': 't1',
            'email': 'a@b.com'
        })

    def test_login_normal(self):
        rubro = SimpleNamespace(nombre='IT')
        user = SimpleNamespace(id=2, email='b@c.com', name='B', token='t2', rubro=rubro, rubro_id=5, rol='usuario', empresa_id=None, ticket_categorias='')
        with patch('routes.auth.login_o_crear_usuario', return_value=user), \
             patch('routes.auth.es_rubro_publico', lambda r: False):
            resp = self.client.post('/google-login', json={'id_token': 'tok'})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['id'], 2)
        self.assertNotIn('status', data)
        self.assertEqual(data['rubro'], 'IT')

if __name__ == '__main__':
    unittest.main()
