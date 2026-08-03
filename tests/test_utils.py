import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Stub CatalogoItem with simple query behaviour
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

from services.common_utils import generar_link_google_maps
import services.herramientas_pyme as hp
from app import create_app, db
from config import TestConfig

class UtilsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()


    def test_link_con_direccion(self):
        url = generar_link_google_maps(direccion="Av Siempreviva 742")
        self.assertIn("Av+Siempreviva+742", url)

    def test_link_con_coordenadas(self):
        url = generar_link_google_maps(latitud=-34.5, longitud=-58.4)
        self.assertEqual(url, "https://www.google.com/maps/search/?api=1&query=-34.5,-58.4")

class StockTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.catalogo_patch = patch.object(hp, 'CatalogoItem', DummyCatalogoItem)
        self.catalogo_patch.start()
        self.addCleanup(self.catalogo_patch.stop)
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
        db.session.remove()
        db.drop_all()
        self.app_context.pop()


    def test_sugerencia_stock(self):
        resp = self.verificar_stock_producto("destornilador", user_id=1)
        self.assertIn("Destornillador", resp)

    def test_sin_stock_alternativa(self):
        resp = self.verificar_stock_producto("Pinza", user_id=1)
        self.assertIn("no tenemos stock", resp.lower())

if __name__ == '__main__':
    unittest.main()
