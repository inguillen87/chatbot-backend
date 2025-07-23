import unittest
from unittest.mock import patch
from types import SimpleNamespace
from app import create_app, db
from config import TestConfig
from models import User, Rubro, MunicipioTicket
from routes.ticket import get_tickets_del_usuario

class TicketsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.app_context.pop()

    def test_get_tickets_del_usuario_admin_municipio(self):
        with self.app.app_context():
            # Create a mock user and rubro
            rubro = Rubro(nombre='municipios', clave='municipios')
            db.session.add(rubro)
            db.session.commit()
            user = User(
                id=1,
                name='Test User',
                email='test@example.com',
                password_hash='test',
                rol='admin',
                municipio_id=10,
                rubro_id=rubro.id
            )
            user.tipo_chat = 'municipio'
            db.session.add(user)
            db.session.commit()

            # Create a mock ticket
            ticket = MunicipioTicket(
                id=1,
                user_id=user.id,
                nro_ticket=101,
                asunto='Test Ticket 1',
                estado='nuevo',
                categoria='Plazas y parques',
                direccion='Calle Falsa 123',
                pregunta='test',
                municipio_id=10
            )
            db.session.add(ticket)
            db.session.commit()

            with patch('routes.ticket.request', SimpleNamespace(args={})), \
                 patch('routes.ticket.current_user', user), \
                 patch('routes.ticket.jsonify') as mock_jsonify:
                # Call the endpoint
                get_tickets_del_usuario()
                # Assertions
                mock_jsonify.assert_called_once()
                args, kwargs = mock_jsonify.call_args
                self.assertEqual(len(args[0]['tickets']), 1)
                self.assertEqual(args[0]['tickets'][0]['nro_ticket'], 101)
