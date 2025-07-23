import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
import sys
from models import User, Rubro, CatalogoItem
import importlib
import services.herramientas_pyme as hp

# Mocking the models module
models_stub = sys.modules.get('models')
if models_stub is None:
    models_stub = SimpleNamespace()
    sys.modules['models'] = models_stub

class UtilsTestCase(unittest.TestCase):

    def setUp(self):
        self.original_models_module = sys.modules.get('models')
        sys.modules['models'] = models_stub
        importlib.reload(hp)
        self.hp = hp
        self.verificar_stock_producto = hp.verificar_stock_producto
        self.armar_link_google_maps = hp.armar_link_google_maps

    def tearDown(self):
        sys.modules['models'] = self.original_models_module
        importlib.reload(hp)

    def test_link_con_direccion(self):
        user = User(direccion="Av. Corrientes 1234", ciudad="CABA")
        link = self.armar_link_google_maps(user)
        self.assertIn("Av.+Corrientes+1234", link)
        self.assertIn("CABA", link)

    def test_link_con_coordenadas(self):
        user = User(latitud=1.23, longitud=4.56)
        link = self.armar_link_google_maps(user)
        self.assertIn("1.23,4.56", link)

if __name__ == '__main__':
    unittest.main()
