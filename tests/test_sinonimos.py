import unittest
from services import sinonimos
from services.sinonimos import aplicar_sinonimos, PRODUCT_SYNONYMS

class SinonimosPymeTests(unittest.TestCase):
    def test_vinos_malbec(self):
        texto = aplicar_sinonimos("vinos malbec", PRODUCT_SYNONYMS)
        self.assertEqual(texto, "malbec")

    def test_camiseta_a_remera(self):
        texto = aplicar_sinonimos("camiseta de algodon", PRODUCT_SYNONYMS)
        self.assertEqual(texto, "remera de algodon")

    def test_loader_trae_diccionario(self):
        datos = sinonimos.cargar_product_synonyms()
        self.assertIn("malbec", datos)
        self.assertIn("remera", datos)

if __name__ == '__main__':
    unittest.main()
