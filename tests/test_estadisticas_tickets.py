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

class EstadisticasTicketsRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Create a user and a token for authentication
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

    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_returns_heatmap(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": 1, "lng": 2}, "weight": 3}
        ]

        response = self.client.get(
            '/estadisticas/tickets?tipo=municipio',
            headers={'Authorization': f'Bearer {self.token}'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"heatmap": [{"location": {"lat": 1, "lng": 2}, "weight": 3}]},
        )
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=1,
            rubro_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            estado=None,
            satisfactorio=None,
        )

if __name__ == '__main__':
    unittest.main()
