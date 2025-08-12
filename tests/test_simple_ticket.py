import unittest
import uuid
from app import create_app, db
from models import PymeTicket
from config import TestConfig

class SimpleTicketTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_create_ticket(self):
        ticket_data = {
            'pregunta': '¿Cuál es el horario de atención?',
            'nro_ticket': int(uuid.uuid4().int % 100000), # Generate a random ticket number
            'user_id': 1 # Assuming a user with id 1 exists
        }
        ticket = PymeTicket(**ticket_data)
        db.session.add(ticket)
        db.session.commit()

        # Retrieve the ticket from the database
        retrieved_ticket = PymeTicket.query.get(ticket.id)
        self.assertIsNotNone(retrieved_ticket)
        self.assertEqual(retrieved_ticket.pregunta, ticket_data['pregunta'])

    def test_ticket_defaults(self):
        ticket_data = {
            'pregunta': 'Consulta de stock',
            'nro_ticket': int(uuid.uuid4().int % 100000),
            'user_id': 1
        }
        ticket = PymeTicket(**ticket_data)
        db.session.add(ticket)
        db.session.commit()

        self.assertEqual(ticket.estado, 'nuevo')
        self.assertIsNone(ticket.asunto)

if __name__ == '__main__':
    unittest.main()
