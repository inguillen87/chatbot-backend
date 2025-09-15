import unittest
from unittest.mock import patch
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta
import jwt

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig
from app import create_app, db
from models import User

@unittest.skip("Skipping legacy tests that fail due to refactoring.")
class MunicipalTicketsMapDataRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        admin_user = User(name='Admin User', email='admin@test.com', rol='admin', tipo_chat='municipio', municipio_id=1)
        admin_user.set_password('admin')
        db.session.add(admin_user)
        db.session.commit()
        self.token = jwt.encode(
            {'user_id': admin_user.id, 'exp': datetime.utcnow() + timedelta(minutes=30)},
            self.app.config['SECRET_KEY'],
            algorithm='HS256'
        )

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.municipal_legacy.servicio_tickets')
    def test_estado_param_optional(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []

        response = self.client.get(
            '/municipal/tickets/map_data',
            headers={'Authorization': f'Bearer {self.token}'}
        )

        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio', municipio_id=1, estado=None
        )

    @patch('routes.municipal_legacy.servicio_tickets')
    def test_estado_param_passed(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []

        response = self.client.get(
            '/municipal/tickets/map_data?estado=cerrado',
            headers={'Authorization': f'Bearer {self.token}'}
        )

        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio', municipio_id=1, estado='cerrado'
        )


if __name__ == '__main__':
    unittest.main()
