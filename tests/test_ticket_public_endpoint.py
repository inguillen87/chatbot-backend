import unittest
from unittest.mock import patch
from app import create_app, db
from models import MunicipioTicket, User, TicketComentario
from utils.auth_helpers import generar_token

class TicketPublicEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()
        # create minimal user to satisfy foreign key
        user = User(name='Admin', email='admin@example.com', rol='admin', tipo_chat='municipio')
        user.set_password('pass')
        db.session.add(user)
        db.session.commit()
        self.user = user

        # create sample ticket linked to the user
        ticket = MunicipioTicket(nro_ticket='123456', municipio_id=user.id, pregunta='p', consulta_pin='654321')
        db.session.add(ticket)
        db.session.commit()
        self.ticket_id = ticket.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_lookup_by_number(self):
        # add timeline data
        comentario = TicketComentario(municipio_ticket_id=self.ticket_id, comentario='primer mensaje')
        cambio_estado = TicketComentario(
            municipio_ticket_id=self.ticket_id,
            comentario="Estado actualizado a 'en progreso'",
            es_admin=True,
            origen='sistema',
            estado_ticket='en progreso'
        )
        db.session.add_all([comentario, cambio_estado])
        db.session.commit()

        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['id_ticket'], 'M-123456')
        self.assertEqual(len(data['timeline']), 3)
        self.assertEqual(data['timeline'][0]['tipo'], 'ticket_creado')
        self.assertEqual(data['timeline'][0]['estado'], 'nuevo')
        self.assertEqual(data['timeline'][1]['tipo'], 'comentario')
        self.assertEqual(data['timeline'][2]['estado'], 'en progreso')

    def test_timeline_maps_cerrado_to_resuelto(self):
        cambio_estado = TicketComentario(
            municipio_ticket_id=self.ticket_id,
            comentario="Estado actualizado a 'cerrado'",
            es_admin=True,
            origen='sistema',
            estado_ticket='cerrado'
        )
        db.session.add(cambio_estado)
        db.session.commit()

        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['timeline'][1]['estado'], 'resuelto')

    @patch('routes.ticket.verify_recaptcha', return_value=False)
    def test_public_lookup_invalid_recaptcha(self, mock_recaptcha):
        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321&recaptcha_token=test')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data["error"], "Verificación reCAPTCHA fallida.")

    def test_public_lookup_ignores_undefined_recaptcha(self):
        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321&recaptcha_token=undefined')
        self.assertEqual(resp.status_code, 200)

    def test_public_lookup_requires_pin(self):
        resp = self.client.get('/tickets/municipio/por_numero/123456')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data["error"], "PIN requerido.")

    def test_authenticated_lookup_without_recaptcha_or_pin(self):
        token = generar_token(self.user.id, self.user.rol, self.user.tipo_chat, self.user.municipio_id, self.user.pyme_id)
        headers = {'Authorization': f'Bearer {token}'}
        resp = self.client.get('/tickets/municipio/por_numero/123456', headers=headers)
        self.assertEqual(resp.status_code, 200)

if __name__ == '__main__':
    unittest.main()
