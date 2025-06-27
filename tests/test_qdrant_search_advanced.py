import unittest
from types import SimpleNamespace
from unittest.mock import patch

import services.qdrant_search as qs


class QdrantAdvancedSearchTests(unittest.TestCase):
    def test_returns_results_when_score_high(self):
        dummy = SimpleNamespace(score=0.3)
        with patch('services.qdrant_search.buscar_catalogo_qdrant', return_value=[dummy]):
            res, sug = qs.buscar_catalogo_avanzado(user_id=1, pregunta='vino')
        self.assertEqual(res, [dummy])
        self.assertIsNone(sug)

    def test_fallback_when_no_results(self):
        with patch('services.qdrant_search.buscar_catalogo_qdrant', return_value=[]):
            with patch('services.qdrant_search.inferir_intencion_con_llm', return_value='ofertas'):
                res, sug = qs.buscar_catalogo_avanzado(user_id=1, pregunta='vinito')
        self.assertEqual(res, [])
        self.assertEqual(sug, 'ofertas')

    def test_infer_when_low_score(self):
        dummy = SimpleNamespace(score=0.1)
        with patch('services.qdrant_search.buscar_catalogo_qdrant', return_value=[dummy]):
            with patch('services.qdrant_search.inferir_intencion_con_llm', return_value='combos'):
                res, sug = qs.buscar_catalogo_avanzado(user_id=2, pregunta='vino barato')
        self.assertEqual(res, [dummy])
        self.assertEqual(sug, 'combos')


if __name__ == '__main__':
    unittest.main()
