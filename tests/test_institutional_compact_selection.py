"""Compact selection: a small catalogue for low-cost providers, ids of published nodes only."""
import json
import unittest

from services.institutional_assistant_content import compact_selection
from tests.test_institutional_reserve_selection import BUNDLE


class CompactSelectionTest(unittest.TestCase):
    def test_request_is_small_and_never_offers_the_start_menu(self):
        seen = {}

        def generate(system, user):
            seen['system'], seen['user'] = system, json.loads(user)
            return '```json\n{"node_ids": ["cud"]}\n```'

        self.assertEqual(compact_selection(BUNDLE, '¿Qué necesito para el certificado?', 'start', generate), ['cud'])
        self.assertNotIn('start', [item['id'] for item in seen['user']['temas']])
        self.assertTrue(all(len(item['texto']) <= 140 for item in seen['user']['temas']))
        self.assertNotIn('tema_actual', seen['user'])

    def test_anything_but_published_ids_falls_back_to_the_main_path(self):
        for answer in ('', 'no sé', '{"node_ids": ["inventado"]}', '{"node_ids": ["start"]}', '{"node_ids": "cud"}',
                       '{"node_ids": ["cud", "cud"]}', None):
            self.assertIsNone(compact_selection(BUNDLE, 'consulta', 'start', lambda *_: answer), answer)

    def test_an_empty_choice_is_a_valid_answer(self):
        self.assertEqual(compact_selection(BUNDLE, 'cuánto sale el dólar', 'cud', lambda *_: '{"node_ids": []}'), [])


if __name__ == '__main__':
    unittest.main()
