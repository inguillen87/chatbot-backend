import unittest
import sys
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from models import db, User, MunicipioTicket, Rubro, TenantProfile

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
                rubro_id=rubro.id
            )
            user.tipo_chat = 'municipio'
            db.session.add(user)
            db.session.flush()
            tenant = TenantProfile(
                slug='tickets-endpoint-municipio',
                nombre='Tickets Endpoint Municipio',
                tipo='municipio',
                municipio_id=user.id,
                is_active=True,
            )
            db.session.add(tenant)
            db.session.flush()
            user.municipio_id = user.id
            user.tenant_id = tenant.id
            user.tenant_slug = tenant.slug
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
                municipio_id=user.id,
                tenant_id=tenant.id,
            )
            db.session.add(ticket)
            db.session.commit()

            with self.app.test_request_context('/tickets'):
                resp = get_tickets_del_usuario_logic(user)
                self.assertEqual(resp.status_code, 200)
                data = resp.get_json()
                self.assertIn('tickets', data)
                self.assertIsInstance(data['tickets'], list)
                self.assertEqual(len(data['tickets']), 1)
                self.assertEqual(data['tickets'][0]['id'], 1)
                self.assertEqual(data['tickets'][0]['nro_ticket'], 'M-101')
                self.assertEqual(data['tickets'][0]['asunto'], 'Test Ticket 1')

    def test_get_tickets_per_page_zero_returns_all(self):
        with self.app.app_context():
            rubro = Rubro(nombre='municipios', clave='municipios')
            db.session.add(rubro)
            db.session.commit()

            user = User(
                id=2,
                name='Paginated User',
                email='paginate@example.com',
                password_hash='test',
                rol='admin',
                rubro_id=rubro.id
            )
            user.tipo_chat = 'municipio'
            db.session.add(user)
            db.session.flush()
            tenant = TenantProfile(
                slug='tickets-endpoint-pagination',
                nombre='Tickets Endpoint Pagination',
                tipo='municipio',
                municipio_id=user.id,
                is_active=True,
            )
            db.session.add(tenant)
            db.session.flush()
            user.municipio_id = user.id
            user.tenant_id = tenant.id
            user.tenant_slug = tenant.slug
            db.session.commit()

            tickets = [
                MunicipioTicket(
                    id=index,
                    user_id=user.id,
                    nro_ticket=200 + index,
                    asunto=f'Ticket {index}',
                    estado='nuevo',
                    categoria='General',
                    direccion='Calle 1',
                    pregunta='test',
                    municipio_id=user.id,
                    tenant_id=tenant.id,
                )
                for index in range(1, 4)
            ]
            db.session.add_all(tickets)
            db.session.commit()

            with self.app.test_request_context('/tickets?per_page=0'):
                resp = get_tickets_del_usuario_logic(user)
                self.assertEqual(resp.status_code, 200)
                data = resp.get_json()
                self.assertEqual(len(data['tickets']), 3)
                self.assertEqual(data['pagination']['per_page'], 0)
                self.assertFalse(data['pagination']['has_next'])
                self.assertFalse(data['pagination']['has_prev'])

if __name__ == '__main__':
    unittest.main()
