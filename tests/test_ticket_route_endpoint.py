import unittest
from unittest.mock import patch
from app import create_app
from models import db, User, MunicipioTicket
from utils.auth_helpers import generar_token
from config import TestingConfig

class TicketRouteEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            municipio = User(id=1, name='Muni', email='m@e.com', password_hash='x', tipo_chat='municipio', latitud=-34.6, longitud=-58.45)
            db.session.add(municipio)
            ticket = MunicipioTicket(id=1, pregunta='p', municipio_id=1, latitud=-34.61, longitud=-58.44)
            db.session.add(ticket)
            db.session.commit()
            self.token = generar_token(1, 'usuario', 'municipio', municipio_id=1, pyme_id=None)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @patch('routes.ticket.obtener_ruta')
    def test_route_endpoint_returns_path(self, mock_ruta):
        mock_ruta.return_value = {
            'ruta': [[-34.6, -58.45], [-34.61, -58.44]],
            'distancia_m': 1000,
            'duracion_s': 120
        }
        headers = {'Authorization': f'Bearer {self.token}'}
        resp = self.client.get('/tickets/municipio/1/ruta', headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('ruta', data)
        self.assertEqual(data['destino']['lat'], -34.61)

if __name__ == '__main__':
    unittest.main()
