import unittest
from types import ModuleType, SimpleNamespace
import sys

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
sys.modules['models'] = models_stub

from services.utils import generar_link_google_maps
import services.herramientas_pyme as hp
hp.CatalogoItem = DummyCatalogoItem
verificar_stock_producto = hp.verificar_stock_producto

class UtilsTestCase(unittest.TestCase):
    def test_link_con_direccion(self):
        url = generar_link_google_maps(direccion="Av Siempreviva 742")
        self.assertIn("Av+Siempreviva+742", url)

    def test_link_con_coordenadas(self):
        url = generar_link_google_maps(latitud=-34.5, longitud=-58.4)
        self.assertEqual(url, "https://www.google.com/maps/search/?api=1&query=-34.5,-58.4")

class StockTestCase(unittest.TestCase):
    def setUp(self):
        DummyCatalogoItem.query.clear()
        DummyCatalogoItem.query.extend([
            DummyItem(nombre="Destornillador", cantidad="5"),
            DummyItem(nombre="Martillo", cantidad="2"),
            DummyItem(nombre="Pinza", cantidad="0"),
        ])

    def test_sugerencia_stock(self):
        resp = verificar_stock_producto("destornilador", user_id=1)
        self.assertIn("Destornillador", resp)

    def test_sin_stock_alternativa(self):
        resp = verificar_stock_producto("Pinza", user_id=1)
        self.assertIn("no tenemos stock", resp.lower())

if __name__ == '__main__':
    unittest.main()
