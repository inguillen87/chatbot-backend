import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import create_app, db
from models import User, Rubro
from routes.auth import dashboard_info
from config import TestConfig as TestingConfig

class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_admin_municipio_panels(self):
        rubro = Rubro(nombre='municipios', clave='municipios')
        user = User(
            id=1,
            rol='admin',
            municipio_id=5,
            rubro=rubro,
            tipo_chat=None,
            email='test@test.com',
            name='test'
        )
        user.set_password('test')
        db.session.add(rubro)
        db.session.add(user)
        db.session.commit()

        with self.app.test_request_context():
            with patch('routes.auth.es_rubro_publico', lambda r: True), \
                 patch('routes.auth.jsonify', lambda x: x):
                resp = dashboard_info.__wrapped__(user)
        self.assertIn('municipio', resp['panels'])
        self.assertIn('crm', resp['panels'])
        self.assertIn('empleados', resp['panels'])
        self.assertIn('pedidos', resp['panels'])
        self.assertEqual(resp['tipo_chat'], 'municipio')

if __name__ == '__main__':
    unittest.main()
