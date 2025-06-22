import unittest
import sys
from types import SimpleNamespace, ModuleType

# Stub models and DB session
models_stub = ModuleType('models')
class _DummySession:
    def __init__(self):
        self.added = []
    def add(self, obj):
        self.added.append(obj)
    def commit(self):
        pass
    def rollback(self):
        pass
class _DummyQuery:
    def __init__(self, items=None):
        self.items = items or []
    def all(self):
        return self.items

models_stub.MunicipioTicket = type('MunicipioTicket', (), {'query': _DummyQuery()})
models_stub.PymeTicket = type('PymeTicket', (), {'query': _DummyQuery()})
models_stub.TicketComentario = type('TicketComentario', (), {})
class DummySurvey(SimpleNamespace):
    pass
models_stub.TicketSatisfaccion = DummySurvey
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub

import services.ticket_service as ts
ts.TicketSatisfaccion = DummySurvey
ServicioTickets = ts.ServicioTickets
ts.MunicipioTicket = models_stub.MunicipioTicket
ts.PymeTicket = models_stub.PymeTicket

class TicketServiceTests(unittest.TestCase):
    def test_guardar_encuesta_crea_objeto(self):
        service = ServicioTickets()
        encuesta = service.guardar_encuesta(1, 'municipio', 5, 'ok')
        self.assertIsNotNone(encuesta)
        self.assertEqual(encuesta.puntuacion, 5)

    def test_obtener_tickets_abiertos_con_ubicacion(self):
        t1 = SimpleNamespace(id=1, estado='nuevo', latitud=1.0, longitud=2.0, categoria='bache', direccion='a')
        t2 = SimpleNamespace(id=2, estado='cerrado', latitud=3.0, longitud=4.0)
        models_stub.MunicipioTicket.query = _DummyQuery([t1, t2])
        service = ServicioTickets()
        res = service.obtener_tickets_abiertos_con_ubicacion('municipio')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

if __name__ == '__main__':
    unittest.main()
