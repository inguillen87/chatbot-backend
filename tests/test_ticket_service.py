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
models_stub.MunicipioTicket = type('MunicipioTicket', (), {})
models_stub.PymeTicket = type('PymeTicket', (), {})
models_stub.TicketComentario = type('TicketComentario', (), {})
class DummySurvey(SimpleNamespace):
    pass
models_stub.TicketSatisfaccion = DummySurvey
models_stub.db = SimpleNamespace(session=_DummySession())
sys.modules['models'] = models_stub

import services.ticket_service as ts
ts.TicketSatisfaccion = DummySurvey
ServicioTickets = ts.ServicioTickets

class TicketServiceTests(unittest.TestCase):
    def test_guardar_encuesta_crea_objeto(self):
        service = ServicioTickets()
        encuesta = service.guardar_encuesta(1, 'municipio', 5, 'ok')
        self.assertIsNotNone(encuesta)
        self.assertEqual(encuesta.puntuacion, 5)

if __name__ == '__main__':
    unittest.main()
