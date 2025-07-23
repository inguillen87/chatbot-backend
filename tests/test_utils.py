import unittest
from types import ModuleType, SimpleNamespace
import sys
import os
import importlib

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Stub models.CatalogoItem with simple query behaviour
models_stub = ModuleType('models')
class DummyQuery(list):
    def filter(self, *a, **k):
        return self
    def all(self):
        return list(self)

class DummyItem(SimpleNamespace):
    pass

class DummyCatalogoItem:
    query = DummyQuery()
    user_id = None
    nombre = None

models_stub.CatalogoItem = DummyCatalogoItem
models_stub.User = type('User', (), {}) # Add dummy User to the stub
# sys.modules['models'] = models_stub # Moved to setUp/tearDown

# Import after potential sys.path modification, but before test class for global names if needed
from services.common_utils import generar_link_google_maps
# Import 'hp' and 'verificar_stock_producto' inside setUp or test methods if they depend on the stub
# import services.herramientas_pyme as hp
# verificar_stock_producto = hp.verificar_stock_producto

class UtilsTestCase(unittest.TestCase):
    def setUp(self):
        self.original_models_module = sys.modules.get('models')
        sys.modules['models'] = models_stub
        import services.herramientas_pyme as hp
        import importlib
        importlib.reload(hp)
        self.hp = hp


    def tearDown(self):
        if self.original_models_module:
            sys.modules['models'] = self.original_models_module
        else:
            del sys.modules['models']
        # Important to reload hp again to restore its state if it's used by other tests
        # or ensure it's imported fresh by other tests.
        import services.herramientas_pyme as hp # Reload to original state
        importlib.reload(hp)


    def test_link_con_direccion(self):
        url = generar_link_google_maps(direccion="Av Siempreviva 742")
        self.assertIn("Av+Siempreviva+742", url)

    def test_link_con_coordenadas(self):
        url = generar_link_google_maps(latitud=-34.5, longitud=-58.4)
        self.assertEqual(url, "https://www.google.com/maps/search/?api=1&query=-34.5,-58.4")

class StockTestCase(unittest.TestCase):
    def setUp(self):
        self.original_models_module = sys.modules.get('models')
        sys.modules['models'] = models_stub
        import services.herramientas_pyme as hp # Ensure hp uses the stubbed models
        import importlib # Ensure importlib is available
        importlib.reload(hp)
        self.hp = hp
        self.verificar_stock_producto = hp.verificar_stock_producto

        # Setup mock data for DummyCatalogoItem.query
        # Ensure DummyCatalogoItem.query is reset and populated correctly for each test
        # This was missing the assignment to the class attribute, it was modifying a local list.
        DummyCatalogoItem.query = DummyQuery() # Reset query for each test
        DummyCatalogoItem.query.extend([
            DummyItem(nombre="Destornillador", cantidad="5"),
            DummyItem(nombre="Martillo", cantidad="2"),
            DummyItem(nombre="Pinza", cantidad="0"),
        ])

    def tearDown(self):
        if self.original_models_module:
            sys.modules['models'] = self.original_models_module
        else:
            if 'models' in sys.modules: # Only delete if it was set by this test
                 del sys.modules['models']
        import services.herramientas_pyme as hp # Reload to original state
        import importlib
        importlib.reload(hp)


    def test_sugerencia_stock(self):
        resp = self.verificar_stock_producto("destornilador", user_id=1)
        self.assertIn("Destornillador", resp)

    def test_sin_stock_alternativa(self):
        resp = self.verificar_stock_producto("Pinza", user_id=1)
        self.assertIn("no tenemos stock", resp.lower())

if __name__ == '__main__':
    unittest.main()
