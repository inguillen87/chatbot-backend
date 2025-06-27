import unittest
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

# Stub models and DB session template
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

import importlib
import services.ticket_service as ts
ServicioTickets = ts.ServicioTickets

class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        # Patch the 'models' module so ServicioTickets imports our dummy models
        self.mod_patch = patch.dict(sys.modules, {'models': models_stub})
        self.mod_patch.start()
        importlib.reload(ts)

    def tearDown(self):
        self.mod_patch.stop()
        importlib.reload(ts)

    def test_guardar_encuesta_crea_objeto(self):
        service = ServicioTickets()
        encuesta = service.guardar_encuesta(1, 'municipio', 5, 'ok')
        self.assertIsNotNone(encuesta)
        self.assertEqual(encuesta.puntuacion, 5)

    def test_mapa_filtra_por_municipio(self):
        DummyTicket = SimpleNamespace

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery([t for t in self if all(getattr(t, k) == v for k, v in kwargs.items())])
            def all(self):
                return list(self)

        t1 = DummyTicket(id=1, estado='abierto', latitud=1, longitud=2, municipio_id=5)
        t2 = DummyTicket(id=2, estado='abierto', latitud=3, longitud=4, municipio_id=6)

        class DummyModel:
            query = DummyQuery([t1, t2])

        with patch.object(ts, 'MunicipioTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_abiertos_con_ubicacion('municipio', municipio_id=5)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

    def test_mapa_filtra_por_rubro(self):
        DummyTicket = SimpleNamespace

        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery([t for t in self if all(getattr(t, k) == v for k, v in kwargs.items())])
            def all(self):
                return list(self)

        t1 = DummyTicket(id=1, estado='abierto', latitud=1, longitud=2, rubro_id=5)
        t2 = DummyTicket(id=2, estado='abierto', latitud=3, longitud=4, rubro_id=7)

        class DummyModel:
            query = DummyQuery([t1, t2])

        with patch.object(ts, 'PymeTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_abiertos_con_ubicacion('pyme', rubro_id=5)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['id'], 1)

if __name__ == '__main__':
    unittest.main()
