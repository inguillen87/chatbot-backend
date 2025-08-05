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

    def test_categoria_filter_pyme(self):
        pyme_user = User(email='pyme@test.com', name='Pyme Test', rol='admin', rubro_id=7, tipo_chat='pyme')
        pyme_user.set_password('password')
        db.session.add(pyme_user)
        db.session.commit()

        t1 = PymeTicket(id=1, nro_ticket=1, estado='abierto', fecha=datetime.now(), categoria='X', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7)
        t2 = PymeTicket(id=2, nro_ticket=2, estado='abierto', fecha=datetime.now(), categoria='Y', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7)
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?categoria=X'):
            response = get_tickets_del_usuario_logic(pyme_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_municipio_user_without_id_returns_error(self):
        user = User(id=9, rol='admin', municipio_id=None, tipo_chat='municipio')
        with self.app.test_request_context():
            response = get_tickets_del_usuario_logic(user)
            self.assertEqual(response.status_code, 400)

if __name__ == '__main__':
    unittest.main()
