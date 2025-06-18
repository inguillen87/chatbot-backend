import unittest
from services.herramientas_municipio import normalizar_texto

class NormalizationTests(unittest.TestCase):
    def test_remove_accents_and_punctuation(self):
        self.assertEqual(normalizar_texto('Árboles, caídos!!!'), 'arbol caido')

    def test_plural_handling(self):
        self.assertEqual(normalizar_texto('Registros'), 'registro')

if __name__ == '__main__':
    unittest.main()
