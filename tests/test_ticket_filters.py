import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from app import create_app, db
from config import TestConfig
from routes.ticket import get_tickets_del_usuario

class TicketFiltersTests(unittest.TestCase):
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

    def test_estado_filter_municipio(self):
        t1 = SimpleNamespace(id=1, nro_ticket=1, estado='abierto', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=5)
        t2 = SimpleNamespace(id=2, nro_ticket=2, estado='cerrado', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=5)
        CategoriaCol = type('Categoria', (), {'__eq__': lambda self, other: ('categoria', other)})
        MunicipioMock = SimpleNamespace(
            query=MagicMock(),
            fecha=SimpleNamespace(desc=lambda: None),
            categoria=CategoriaCol(),
        )
        MunicipioMock.query.filter.return_value.filter.return_value.order_by.return_value.paginate.return_value.items = [t1]
        user = SimpleNamespace(id=1, rol='admin', municipio_id=5, rubro=SimpleNamespace(nombre='municipios'), ticket_categorias='')
        with patch('routes.ticket.request', SimpleNamespace(args={'estado': 'abierto'})):
            with patch('routes.ticket.MunicipioTicket', MunicipioMock), patch('routes.ticket.jsonify', lambda x: x):
                res = get_tickets_del_usuario(user)
                self.assertEqual(len(res['tickets']), 1)
                self.assertEqual(res['tickets'][0]['estado'], 'abierto')

    def test_categoria_filter_pyme(self):
        t1 = SimpleNamespace(id=1, nro_ticket=1, estado='abierto', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='X', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7)
        t2 = SimpleNamespace(id=2, nro_ticket=2, estado='abierto', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='Y', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7)
        CategoriaCol = type('Categoria', (), {'__eq__': lambda self, other: ('categoria', other)})
        PymeMock = SimpleNamespace(
            query=MagicMock(),
            fecha=SimpleNamespace(desc=lambda: None),
            categoria=CategoriaCol(),
        )
        PymeMock.query.filter.return_value.filter.return_value.order_by.return_value.paginate.return_value.items = [t1]
        user = SimpleNamespace(id=1, rol='admin', rubro_id=7, rubro=SimpleNamespace(nombre='pyme'), ticket_categorias='')
        with patch('routes.ticket.request', SimpleNamespace(args={'categoria': 'X'})):
            with patch('routes.ticket.PymeTicket', PymeMock), patch('routes.ticket.jsonify', lambda x: x):
                res = get_tickets_del_usuario(user)
                self.assertEqual(len(res['tickets']), 1)
                self.assertEqual(res['tickets'][0]['categoria'], 'X')

    def test_municipio_user_without_id_returns_error(self):
        user = SimpleNamespace(id=9, rol='admin', municipio_id=None, rubro=SimpleNamespace(nombre='municipios'), ticket_categorias='')
        with patch('routes.ticket.request', SimpleNamespace(args={})), \
             patch('routes.ticket.jsonify', lambda x: x):
            res, status = get_tickets_del_usuario(user)
            self.assertEqual(status, 400)
            self.assertIn('ID de municipio no encontrado', res['error'])
