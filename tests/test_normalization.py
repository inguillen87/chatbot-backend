import unittest
from services.herramientas_municipio import normalizar_texto
from services.common_utils import unir_codigos_alfa_numericos # Changed from services.utils

class NormalizationTests(unittest.TestCase):
    def test_remove_accents_and_punctuation(self):
        self.assertEqual(normalizar_texto('Árboles, caídos!!!'), 'arboles caidos')

    def test_plural_handling(self):
        self.assertEqual(normalizar_texto('Registros'), 'registros')

    def test_unir_codigos_alfa_numericos(self):
        self.assertEqual(unir_codigos_alfa_numericos('de 108 c'), 'de108c')

if __name__ == '__main__':
    unittest.main()
