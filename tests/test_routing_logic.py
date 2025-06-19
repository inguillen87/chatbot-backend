import unittest
from types import ModuleType, SimpleNamespace
import sys
import importlib
from contextlib import contextmanager
import unittest.mock

@contextmanager
def stub_modules():
    cohere_stub = ModuleType('services.cohere_ai')
    cohere_stub.get_cohere_response = lambda *a, **k: ""
    pymes_stub = ModuleType('services.pymes')
    pymes_stub.responder_pyme = lambda *a, **k: {'origen': 'pyme'}
    municipio_stub = ModuleType('services.municipios')
    municipio_stub.responder_municipio = lambda *a, **k: {'origen': 'municipio'}
    mods = {
        'services.cohere_ai': cohere_stub,
        'services.pymes': pymes_stub,
        'services.municipios': municipio_stub,
    }
    with unittest.mock.patch.dict(sys.modules, mods):
        import services.logic as logic
        importlib.reload(logic)
        yield logic

class RoutingLogicTests(unittest.TestCase):
    def test_pyme_routing(self):
        with stub_modules() as logic:
            resp = logic.responder_chatboc('hola', tipo_chat='pyme')
            self.assertEqual(resp['origen'], 'pyme')

    def test_municipio_routing(self):
        with stub_modules() as logic:
            resp = logic.responder_chatboc('hola', tipo_chat='municipio')
            self.assertEqual(resp['origen'], 'municipio')

    def test_tipo_chat_required(self):
        with stub_modules() as logic:
            with self.assertRaises(ValueError):
                logic.responder_chatboc('hola')

    def test_rubro_corrige_a_municipio(self):
        """Si el rubro es de pyme pero viene tipo_chat municipio se corrige."""
        with stub_modules() as logic:
            rubro_obj = SimpleNamespace(nombre='vinoteca', clave='vinoteca')
            resp = logic.responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj)
            self.assertEqual(resp['origen'], 'pyme')

    def test_rubro_sin_nombre_usa_clave(self):
        """Debe usar la clave del rubro cuando no hay nombre."""
        with stub_modules() as logic:
            rubro_obj = SimpleNamespace(nombre=None, clave='vinoteca')
            resp = logic.responder_chatboc('hola', tipo_chat='municipio', rubro_obj=rubro_obj)
            self.assertEqual(resp['origen'], 'pyme')

    def test_rubro_corrige_a_pyme(self):
        """Si el rubro es municipal pero viene tipo_chat pyme se corrige."""
        with stub_modules() as logic:
            rubro_obj = SimpleNamespace(nombre='municipio', clave='municipio')
            resp = logic.responder_chatboc('hola', tipo_chat='pyme', rubro_obj=rubro_obj)
            self.assertEqual(resp['origen'], 'municipio')

    def test_es_rubro_publico_normaliza(self):
        with stub_modules() as logic:
            rubro_obj = SimpleNamespace(nombre='Municipios', clave='municipios')
            self.assertTrue(logic.es_rubro_publico(rubro_obj))
            self.assertTrue(logic.es_rubro_publico('municipios'))
            self.assertEqual(logic.normalizar_rubro(rubro_obj), 'municipios')

if __name__ == '__main__':
    unittest.main()
