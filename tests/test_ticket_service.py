import unittest
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import patch, MagicMock
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

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
models_stub.Conversacion = type('Conversacion', (), {})
class DummySurvey(SimpleNamespace):
    pass
models_stub.TicketSatisfaccion = DummySurvey
models_stub.db = SimpleNamespace(session=_DummySession())
models_stub.db.func = SimpleNamespace(lower=lambda x: x)
class DummyUser:
    email = 'existing@example.com'
    name = 'Existing'
    telefono = '2636160364'
    id = 2
models_stub.User = DummyUser

import importlib
import services.ticket_service as ts
ServicioTickets = ts.ServicioTickets

class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        # Patch the 'models' module so ServicioTickets imports our dummy models
        self.mod_patch = patch.dict(sys.modules, {'models': models_stub})
        self.mod_patch.start()
        importlib.reload(ts) # Reload to make sure it picks up the patched models
        models_stub.db.session.flush = MagicMock()

    def tearDown(self):
        self.mod_patch.stop()
        importlib.reload(ts) # Reload again to restore original imports for other tests

    def test_guardar_encuesta_crea_objeto(self):
        service = ServicioTickets()
        encuesta = service.guardar_encuesta(1, 'municipio', 5, 'ok')
        self.assertIsNotNone(encuesta)
        self.assertEqual(encuesta.puntuacion, 5)

    def test_mapa_filtra_por_municipio(self):
        DummyTicket = SimpleNamespace

        # Mocking the structure that obtener_tickets_con_ubicacion_para_mapa would query
        # This test needs to be adapted if the internal logic of the method changes significantly.
        # For now, we assume it queries and filters.
        class DummyQuery(list):
            def filter_by(self, **kwargs):
                # Simple filter_by mock
                return DummyQuery([t for t in self if all(getattr(t, k, None) == v for k, v in kwargs.items())])
            def filter(self, *criterion): # Add basic filter mock
                # This is a very basic mock for .filter(Model.latitud.isnot(None), ...)
                # It won't actually evaluate complex SQLAlchemy criterion.
                return self
            def all(self):
                return list(self)

        # Sample data that would be in the DB
        from datetime import datetime # Needed for fecha
        t1 = DummyTicket(id=1, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t2 = DummyTicket(id=2, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None) # Same location as t1
        t3 = DummyTicket(id=3, estado='abierto', latitud=11.0, longitud=21.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t4 = DummyTicket(id=4, estado='abierto', latitud=12.0, longitud=22.0, municipio_id=6, fecha=datetime.utcnow(), categoria=None, asunto=None) # Different municipio

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        query = DummyQuery([
            SimpleNamespace(id=1, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=2, estado='abierto', latitud=10.0, longitud=20.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=3, estado='abierto', latitud=11.0, longitud=21.0, municipio_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=4, estado='abierto', latitud=12.0, longitud=22.0, municipio_id=6, fecha=datetime.utcnow(), categoria=None, asunto=None)
        ])
        DummyModel.query = query

        with patch.object(ts, 'MunicipioTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(tipo_ticket='municipio', municipio_id=5)

        # The method now returns a list of dicts with "location", "weight" and "categoria"
        # We expect two distinct locations for municipio_id=5: (10.0, 20.0) with weight 2, and (11.0, 21.0) with weight 1
        self.assertEqual(len(res), 2)

        found_loc1 = False
        found_loc2 = False
        for item in res:
            # Rounding might occur in the service, so compare with tolerance or ensure mock data uses expected precision
            if (
                abs(item['location']['lat'] - 10.0) < 0.0001
                and abs(item['location']['lng'] - 20.0) < 0.0001
                and item['weight'] == 2
                and item.get('categoria') is None
            ):
                found_loc1 = True
            if (
                abs(item['location']['lat'] - 11.0) < 0.0001
                and abs(item['location']['lng'] - 21.0) < 0.0001
                and item['weight'] == 1
                and item.get('categoria') is None
            ):
                found_loc2 = True

        self.assertTrue(found_loc1, "Location (10.0, 20.0) with weight 2 not found")
        self.assertTrue(found_loc2, "Location (11.0, 21.0) with weight 1 not found")

    def test_preserves_user_provided_phone_when_user_exists(self):
        service = ServicioTickets()
        class DummyCreator:
            def __init__(self):
                self.last_ticket_data = None
            def create(self, td):
                self.last_ticket_data = td
                class DummyTicket(SimpleNamespace):
                    def __getattr__(self, name):
                        return None
                return DummyTicket(id=1, nro_ticket=123, asunto='a')

        dummy_creator = DummyCreator()
        service.creators['municipio'] = dummy_creator

        existing_user = SimpleNamespace(id=2, name='Existing', email='existing@example.com', telefono='2636160364')
        class DummyQuery:
            def filter(self, *args, **kwargs):
                return self
            def first(self):
                return existing_user

        models_stub.User.query = DummyQuery()

        ticket_data = {
            'email_vecino': 'existing@example.com',
            'telefono_vecino': '351122395',
            'categoria': 'General',
            'detalles': 'algo'
        }

        service.crear_nuevo_ticket('municipio', ticket_data)
        self.assertEqual(dummy_creator.last_ticket_data['telefono_vecino'], '351122395')


    def test_mapa_filtra_por_rubro(self):
        DummyTicket = SimpleNamespace
        class DummyQuery(list):
            def filter_by(self, **kwargs):
                return DummyQuery([t for t in self if all(getattr(t, k, None) == v for k, v in kwargs.items())])
            def filter(self, *criterion):
                return self
            def all(self):
                return list(self)

        from datetime import datetime # Needed for fecha
        t1 = DummyTicket(id=1, estado='abierto', latitud=10.0, longitud=20.0, rubro_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t2 = DummyTicket(id=2, estado='abierto', latitud=11.0, longitud=21.0, rubro_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None)
        t3 = DummyTicket(id=3, estado='abierto', latitud=12.0, longitud=22.0, rubro_id=7, fecha=datetime.utcnow(), categoria=None, asunto=None) # Different rubro

        class DummyModel:
            latitud = MagicMock()
            latitud.isnot.return_value = True
            longitud = MagicMock()
            longitud.isnot.return_value = True

            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        query = DummyQuery([
            SimpleNamespace(id=1, estado='abierto', latitud=10.0, longitud=20.0, rubro_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=2, estado='abierto', latitud=11.0, longitud=21.0, rubro_id=5, fecha=datetime.utcnow(), categoria=None, asunto=None),
            SimpleNamespace(id=3, estado='abierto', latitud=12.0, longitud=22.0, rubro_id=7, fecha=datetime.utcnow(), categoria=None, asunto=None)
        ])
        DummyModel.query = query
        with patch.object(ts, 'PymeTicket', DummyModel):
            service = ServicioTickets()
            res = service.obtener_tickets_con_ubicacion_para_mapa(tipo_ticket='pyme', rubro_id=5)

        # Expecting two items for rubro_id=5, each with weight 1 as they are distinct locations
        self.assertEqual(len(res), 2)
        self.assertTrue(
            any(
                abs(d['location']['lat'] - 10.0) < 0.0001
                and d['weight'] == 1
                and d.get('categoria') is None
                for d in res
            )
        )
        self.assertTrue(
            any(
                abs(d['location']['lat'] - 11.0) < 0.0001
                and d['weight'] == 1
                and d.get('categoria') is None
                for d in res
            )
        )


if __name__ == '__main__':
    unittest.main()
