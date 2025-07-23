import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_ticket_filters = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_ticket_filters not in sys.path:
    sys.path.insert(0, project_root_ticket_filters)

from routes.ticket import get_tickets_del_usuario_logic

class DummyQuery:
    def __init__(self, items):
        self.items = list(items)
    def filter_by(self, **kwargs):
        self.items = [i for i in self.items if all(getattr(i, k) == v for k, v in kwargs.items())]
        return self
    def filter(self, criterion):
        try:
            if isinstance(criterion, tuple):
                key, val = criterion
            else:
                key = criterion.left.name
                val = criterion.right.value
            self.items = [i for i in self.items if getattr(i, key) == val]
        except Exception:
            pass
        return self
    def order_by(self, *args):
        return self
    def all(self):
        return self.items

class TicketFiltersTests(unittest.TestCase):
    def setUp(self):
        pass

    def test_estado_filter_municipio(self):
        t1 = SimpleNamespace(id=1, nro_ticket=1, estado='abierto', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=5)
        t2 = SimpleNamespace(id=2, nro_ticket=2, estado='cerrado', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=5)
        CategoriaCol = type('Categoria', (), {'__eq__': lambda self, other: ('categoria', other)})
        MunicipioMock = SimpleNamespace(
            query=DummyQuery([t1, t2]),
            fecha=SimpleNamespace(desc=lambda: None),
            categoria=CategoriaCol(),
        )
        user = SimpleNamespace(id=1, rol='admin', municipio_id=5, rubro=SimpleNamespace(nombre='municipios'), ticket_categorias='')
        with patch('routes.ticket.request', SimpleNamespace(args={'estado': 'abierto'})):
            with patch('routes.ticket.MunicipioTicket', MunicipioMock), patch('routes.ticket.jsonify', lambda x: x):
                res = get_tickets_del_usuario_logic(user)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

    def test_categoria_filter_pyme(self):
        t1 = SimpleNamespace(id=1, nro_ticket=1, estado='abierto', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='X', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7)
        t2 = SimpleNamespace(id=2, nro_ticket=2, estado='abierto', fecha=SimpleNamespace(isoformat=lambda:'x'), categoria='Y', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7)
        CategoriaCol = type('Categoria', (), {'__eq__': lambda self, other: ('categoria', other)})
        PymeMock = SimpleNamespace(
            query=DummyQuery([t1, t2]),
            fecha=SimpleNamespace(desc=lambda: None),
            categoria=CategoriaCol(),
        )
        user = SimpleNamespace(id=1, rol='admin', rubro_id=7, rubro=SimpleNamespace(nombre='pyme'), ticket_categorias='')
        with patch('routes.ticket.request', SimpleNamespace(args={'categoria': 'X'})):
            with patch('routes.ticket.PymeTicket', PymeMock), patch('routes.ticket.jsonify', lambda x: x):
                res = get_tickets_del_usuario_logic(user)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

    def test_municipio_user_without_id_returns_error(self):
        user = SimpleNamespace(id=9, rol='admin', municipio_id=None, rubro=SimpleNamespace(nombre='municipios'), ticket_categorias='')
        with patch('routes.ticket.request', SimpleNamespace(args={})), \
             patch('routes.ticket.jsonify', lambda x: x):
            res, status = get_tickets_del_usuario_logic(user)
        self.assertEqual(status, 400)

if __name__ == '__main__':
    unittest.main()
