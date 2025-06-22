import unittest

import services.tramites as ts

class TramitesServiceTests(unittest.TestCase):
    def test_buscar_tramites_ordenados(self):
        ts.TRAMITES_INFO = {
            'b': {},
            'a': {},
            'c': {},
        }
        res = ts.buscar_tramites()
        self.assertEqual([r['nombre'] for r in res], ['a', 'b', 'c'])

    def test_buscar_tramites_filtra(self):
        ts.TRAMITES_INFO = {
            'licencia': {'descripcion': 'x'},
            'impuesto': {'descripcion': 'y'},
        }
        res = ts.buscar_tramites('licencia')
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['nombre'], 'licencia')

if __name__ == '__main__':
    unittest.main()
