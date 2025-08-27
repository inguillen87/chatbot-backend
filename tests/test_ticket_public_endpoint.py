import unittest
from unittest.mock import patch
from app import create_app, db
from models import MunicipioTicket, User

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

        # create sample ticket linked to the user
        ticket = MunicipioTicket(nro_ticket='123456', municipio_id=user.id, pregunta='p')
        db.session.add(ticket)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.ticket.verify_recaptcha', return_value=True)
    def test_public_lookup_by_number(self, mock_recaptcha):
        resp = self.client.get('/tickets/municipio/por_numero/123456?recaptcha_token=test')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['id_ticket'], 'M-123456')

    def test_public_lookup_requires_recaptcha(self):
        resp = self.client.get('/tickets/municipio/por_numero/123456')
        self.assertEqual(resp.status_code, 400)

if __name__ == '__main__':
    unittest.main()
