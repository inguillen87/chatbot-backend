import unittest
from unittest.mock import patch
from app import create_app, db
from models import MunicipioTicket, User, TicketComentario

class TicketProgressRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        user = User(name='Admin', email='admin@example.com', rol='admin', tipo_chat='municipio',
                    latitud=1.0, longitud=1.0)
        user.set_password('pass')
        db.session.add(user)
        db.session.commit()
        self.user = user

        ticket = MunicipioTicket(nro_ticket='123456', municipio_id=user.id, pregunta='p', consulta_pin='654321',
                                 latitud=2.0, longitud=2.0, user_id=user.id)
        db.session.add(ticket)
        db.session.commit()
        self.ticket_id = ticket.id

        comentario_progreso = TicketComentario(municipio_ticket_id=self.ticket_id, comentario='en progreso',
                                               es_admin=True, estado_ticket='en progreso')
        comentario_cerrado = TicketComentario(municipio_ticket_id=self.ticket_id, comentario='cerrado',
                                              es_admin=True, estado_ticket='cerrado')
        db.session.add_all([comentario_progreso, comentario_cerrado])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.ticket.obtener_ruta')
    def test_progress_and_route_included(self, mock_ruta):
        mock_ruta.return_value = {'distancia_m': 100, 'duracion_s': 60, 'ruta': [[-34.1, -58.4]]}
        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('progreso_estados', data)
        self.assertEqual(len(data['progreso_estados']), 3)
        for estado in data['progreso_estados']:
            self.assertTrue(estado['completado'])
        self.assertIn('ruta', data)
        self.assertEqual(data['ruta']['origen']['lat'], 1.0)
        self.assertEqual(data['ruta']['destino']['lat'], 2.0)

if __name__ == '__main__':
    unittest.main()
