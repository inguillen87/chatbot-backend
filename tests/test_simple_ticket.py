import unittest
from unittest.mock import patch
from app import create_app, db
from src.models import User, MunicipioTicket, Rubro
from services.ticket_service import servicio_tickets

class SimpleTicketTest(unittest.TestCase):
    def setUp(self):
        from src.config import TestConfig
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        with self.app.app_context():
            db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_create_municipio_ticket(self):
        # Create a user and a rubro
        rubro = Rubro(nombre='municipios', clave='municipios')
        db.session.add(rubro)
        db.session.commit()
        user = User(name='Test User', email='test@test.com', municipio_id=1, rubro_id=rubro.id)
        user.set_password('password')
        db.session.add(user)
        db.session.commit()

        # Create ticket data
        ticket_data = {
            "asunto": "Test Ticket",
            "categoria": "Test Categoria",
            "detalles": "Test Details",
            "direccion": "Test Address",
            "nombre_vecino": "Test Neighbor",
            "telefono_vecino": "1234567890",
            "email_vecino": "neighbor@test.com",
            "estado": "nuevo",
            "user_id": user.id,
            "latitud": 1.0,
            "longitud": 1.0,
            "origen_reclamo": "TEST"
        }

        # Create the ticket
        ticket_creado = MunicipioTicket(**ticket_data)
        db.session.add(ticket_creado)
        db.session.commit()

        # Assert that the ticket was created successfully
        self.assertIsNotNone(ticket_creado)
        self.assertEqual(ticket_creado.asunto, "Test Ticket")
        self.assertEqual(ticket_creado.user_id, user.id)
