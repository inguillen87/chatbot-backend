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
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False

from models import User, MunicipioTicket, PymeTicket, Rubro
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
        # Create a "municipio" user that acts as the owner
        municipio_owner = User(email='municipio@test.com', name='Municipio Test', rol='municipio')
        municipio_owner.set_password('password')
        db.session.add(municipio_owner)
        db.session.commit()

        admin_user = User(email='admin@test.com', name='Admin Test', rol='admin', municipio_id=municipio_owner.id, tipo_chat='municipio')
        admin_user.set_password('password')
        db.session.add(admin_user)
        db.session.commit()

        t1 = MunicipioTicket(id=1, nro_ticket='1', estado='abierto', fecha=datetime.now(), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=municipio_owner.id)
        t2 = MunicipioTicket(id=2, nro_ticket='2', estado='cerrado', fecha=datetime.now(), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=municipio_owner.id)
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?estado=abierto'):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_categoria_filter_pyme(self):
        rubro = Rubro(clave='bodega', nombre='Bodega')
        db.session.add(rubro)
        db.session.commit()

        pyme_user = User(email='pyme@test.com', name='Pyme Test', rol='admin', rubro_id=rubro.id, tipo_chat='pyme')
        pyme_user.set_password('password')
        db.session.add(pyme_user)
        db.session.commit()

        t1 = PymeTicket(id=1, nro_ticket=1, estado='abierto', fecha=datetime.now(), categoria='X', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=rubro.id, pregunta="pregunta de prueba 1")
        t2 = PymeTicket(id=2, nro_ticket=2, estado='abierto', fecha=datetime.now(), categoria='Y', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=rubro.id, pregunta="pregunta de prueba 2")
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?categoria=X'):
            response = get_tickets_del_usuario_logic(pyme_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_municipio_user_without_id_returns_error(self):
        user = User(id=9, rol='admin', municipio_id=None, tipo_chat='municipio', name="Test User", email="test@user.com")
        user.set_password("password")
        with self.app.test_request_context():
            response, status_code = get_tickets_del_usuario_logic(user)
            self.assertEqual(status_code, 400)
            self.assertIn("El usuario municipal no tiene asignado un municipio_id válido", response.get_json()['error'])

if __name__ == '__main__':
    unittest.main()