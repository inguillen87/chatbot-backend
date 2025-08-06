import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import sys
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from models import db, User, MunicipioTicket, Rubro

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from routes.ticket import get_tickets_del_usuario_logic

class TicketsEndpointTest(unittest.TestCase):
    def setUp(self):
        """Set up a temporary database for the tests."""
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        """Tear down the database."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_get_tickets_del_usuario_municipio(self):
        with self.app.app_context():
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

            with patch('routes.ticket.request', SimpleNamespace(args={})):
                resp = get_tickets_del_usuario_logic(user)
                self.assertEqual(resp.status_code, 200)
                data = resp.get_json()
                self.assertIn('tickets', data)
                self.assertIsInstance(data['tickets'], list)
                self.assertEqual(len(data['tickets']), 1)
                self.assertEqual(data['tickets'][0]['id'], 1)
                self.assertEqual(data['tickets'][0]['nro_ticket'], 'M-101')
                self.assertEqual(data['tickets'][0]['asunto'], 'Test Ticket 1')

if __name__ == '__main__':
    unittest.main()
