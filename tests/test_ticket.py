import sys
import os
import unittest
from unittest.mock import patch
from app import create_app
from extensions import db
from models import User, MunicipioTicket, TicketComentario

class TicketRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SESSION_TYPE'] = 'filesystem'
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_get_chat_mensajes_anon_success(self):
        """
        Tests that an anonymous user can access the chat messages with a valid anon_id.
        """
        with self.app.app_context():
            ticket = MunicipioTicket(anon_id='test-anon-id', estado='nuevo', pregunta='test pregunta', nro_ticket='12345')
            db.session.add(ticket)
            db.session.commit()
            ticket_id = ticket.id

        response = self.client.get(f'/tickets/chat/{ticket_id}/mensajes', headers={'Anon-Id': 'test-anon-id'})
        self.assertEqual(response.status_code, 200)

    def test_get_chat_mensajes_anon_failure(self):
        """
        Tests that an anonymous user cannot access the chat messages with an invalid anon_id.
        """
        with self.app.app_context():
            ticket = MunicipioTicket(anon_id='test-anon-id', estado='nuevo', pregunta='test pregunta', nro_ticket='12345')
            db.session.add(ticket)
            db.session.commit()
            ticket_id = ticket.id

        response = self.client.get(f'/tickets/chat/{ticket_id}/mensajes', headers={'Anon-Id': 'invalid-anon-id'})
        self.assertEqual(response.status_code, 403)

if __name__ == '__main__':
    unittest.main()
