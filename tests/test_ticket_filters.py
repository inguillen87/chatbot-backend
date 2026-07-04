import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os
from app import create_app, db
from config import Config

# Añadir el directorio raíz del proyecto al sys.path
project_root_ticket_filters = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_ticket_filters not in sys.path:
    sys.path.insert(0, project_root_ticket_filters)

from routes.ticket import get_tickets_del_usuario_logic

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class DummyQuery:
    def __init__(self, items):
        self.items = list(items)
    def filter_by(self, **kwargs):
        self.items = [i for i in self.items if all(getattr(i, k) == v for k, v in kwargs.items())]
        return self
    def filter(self, *criterion):
        for c in criterion:
            try:
                if isinstance(c, tuple):
                    key, val = c
                else:
                    key = c.left.name
                    val = c.right.value
                self.items = [i for i in self.items if getattr(i, key) == val]
            except Exception:
                pass
        return self
    def order_by(self, *args):
        return self
    def all(self):
        return self.items
    def offset(self, *args):
        return self
    def limit(self, *args):
        return self

from models import User, MunicipioTicket, PymeTicket
from datetime import datetime

class TicketFiltersTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_estado_filter_municipio(self):
        admin_user = User(email='admin@test.com', name='Admin Test', rol='admin', municipio_id=5, tipo_chat='municipio')
        admin_user.set_password('password')
        db.session.add(admin_user)
        db.session.commit()

        t1 = MunicipioTicket(id=1, nro_ticket='1', estado='abierto', fecha=datetime.now(), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=5)
        t2 = MunicipioTicket(id=2, nro_ticket='2', estado='cerrado', fecha=datetime.now(), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=5)
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?estado=abierto'):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_estado_filter_uses_filtered_pagination_total(self):
        admin_user = User(email='admin-pagination@test.com', name='Admin Pagination', rol='admin', municipio_id=15, tipo_chat='municipio')
        admin_user.set_password('password')
        db.session.add(admin_user)
        db.session.commit()

        tickets = [
            MunicipioTicket(id=11, nro_ticket='P-11', estado='abierto', fecha=datetime.now(), categoria='A', municipio_id=15),
            MunicipioTicket(id=12, nro_ticket='P-12', estado='abierto', fecha=datetime.now(), categoria='A', municipio_id=15),
            MunicipioTicket(id=13, nro_ticket='P-13', estado='cerrado', fecha=datetime.now(), categoria='A', municipio_id=15),
        ]
        db.session.add_all(tickets)
        db.session.commit()

        with self.app.test_request_context('?estado=abierto&per_page=1'), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['pagination']['total_items'], 2)
            self.assertEqual(data['pagination']['total_pages'], 2)
            self.assertTrue(data['pagination']['has_next'])
            self.assertEqual(data['summary']['total'], 3)

    def test_search_finds_ticket_without_user_join_match(self):
        admin_user = User(email='admin-search@test.com', name='Admin Search', rol='admin', municipio_id=16, tipo_chat='municipio')
        admin_user.set_password('password')
        db.session.add(admin_user)
        db.session.commit()

        ticket = MunicipioTicket(id=21, nro_ticket='M-ANON-777', estado='nuevo', fecha=datetime.now(), categoria='Luminaria', municipio_id=16, user_id=None)
        db.session.add(ticket)
        db.session.commit()

        with self.app.test_request_context('?q=ANON-777'), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 21)

    def test_channel_and_agent_filters_municipio(self):
        admin_user = User(email='admin-agent@test.com', name='Admin Agent', rol='admin', municipio_id=17, tipo_chat='municipio')
        assigned_user = User(email='agent@test.com', name='Agent Test', rol='empleado', municipio_id=17, tipo_chat='municipio')
        admin_user.set_password('password')
        assigned_user.set_password('password')
        db.session.add_all([admin_user, assigned_user])
        db.session.commit()

        assigned_ticket = MunicipioTicket(
            id=31,
            nro_ticket='M-ASSIGNED',
            estado='nuevo',
            fecha=datetime.now(),
            categoria='A',
            municipio_id=17,
            canal_ingreso='whatsapp',
            asignado_a_id=assigned_user.id,
        )
        unassigned_ticket = MunicipioTicket(
            id=32,
            nro_ticket='M-UNASSIGNED',
            estado='nuevo',
            fecha=datetime.now(),
            categoria='A',
            municipio_id=17,
            canal_ingreso='web',
            asignado_a_id=None,
        )
        db.session.add_all([assigned_ticket, unassigned_ticket])
        db.session.commit()

        with self.app.test_request_context(f'?channel=whatsapp&assigned_agent={assigned_user.id}'), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 31)

        with self.app.test_request_context('?unassigned=true'), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 32)

    def test_categoria_filter_pyme(self):
        pyme_user = User(email='pyme@test.com', name='Pyme Test', rol='admin', rubro_id=7, tipo_chat='pyme')
        pyme_user.set_password('password')
        db.session.add(pyme_user)
        db.session.commit()

        t1 = PymeTicket(id=1, nro_ticket=1, estado='abierto', fecha=datetime.now(), categoria='X', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7, pregunta="pregunta de prueba 1")
        t2 = PymeTicket(id=2, nro_ticket=2, estado='abierto', fecha=datetime.now(), categoria='Y', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7, pregunta="pregunta de prueba 2")
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?categoria=X'), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(pyme_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_municipio_user_without_id_returns_error(self):
        user = User(id=9, rol='empleado', municipio_id=None, tipo_chat='municipio')
        with self.app.test_request_context(), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(user)
            self.assertEqual(response.status_code, 403)
            data = response.get_json()
            self.assertEqual(data["access_contract"]["contract_version"], "tickets.access.v1")
            self.assertEqual(data["action_hint"], "repair_ticket_scope")
            self.assertEqual(data["reason_code"], "missing_municipal_scope")
            self.assertIn("tickets.read", data["required_capabilities"])
            self.assertEqual(data["current_scope"]["tipo_chat"], "municipio")
            self.assertEqual(response.headers.get("X-Request-Id"), data["request_id"])

    def test_pyme_user_without_scope_returns_repair_contract(self):
        user = User(id=10, rol='admin', municipio_id=None, rubro_id=None, tipo_chat='pyme')
        with self.app.test_request_context(headers={"X-Request-Id": "req-ticket-scope"}), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(user)
            self.assertEqual(response.status_code, 403)
            data = response.get_json()
            self.assertEqual(data["access_contract"]["contract_version"], "tickets.access.v1")
            self.assertEqual(data["action_hint"], "repair_ticket_scope")
            self.assertEqual(data["reason_code"], "missing_pyme_scope")
            self.assertEqual(data["request_id"], "req-ticket-scope")
            self.assertIn("reclamos.read", data["required_capabilities"])
            self.assertEqual(data["current_scope"]["tipo_chat"], "pyme")

if __name__ == '__main__':
    unittest.main()
