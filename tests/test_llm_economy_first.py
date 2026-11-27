"""Economy-first text path: low-cost providers before the primary one, opt-in."""
import os
import unittest
from unittest.mock import patch

from services import llm_bridge


class EconomyFirstTest(unittest.TestCase):
    def _call(self, reserve, env):
        clean = {k: v for k, v in os.environ.items() if k != 'LLM_ECONOMY_FIRST'}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True), \
                patch('services.llm_reserve_providers.generate_reserve_text', side_effect=reserve) as reserve_call, \
                patch('services.openai_bridge._get_openai_responses_client', side_effect=RuntimeError('primary')) as primary:
            text = llm_bridge.llamar_llm_para_generacion_texto('s', 'u', json_output=True)
        return text, reserve_call.call_count, primary.call_count

    def test_off_by_default(self):
        text, reserve_calls, primary_calls = self._call(lambda *a, **k: '{"node_ids": []}', {})
        self.assertEqual((text, reserve_calls, primary_calls), ('', 0, 1))

    def test_valid_json_from_economy_provider_skips_the_primary(self):
        text, reserve_calls, primary_calls = self._call(lambda *a, **k: '```json\n{"node_ids": ["a"]}\n```',
                                                         {'LLM_ECONOMY_FIRST': '1'})
        self.assertEqual((text, reserve_calls, primary_calls), ('{"node_ids": ["a"]}', 1, 0))

    def test_unparseable_or_empty_answer_continues_to_the_primary(self):
        for answer in ('no es json', ''):
            text, reserve_calls, primary_calls = self._call(lambda *a, **k: answer, {'LLM_ECONOMY_FIRST': 'true'})
            self.assertEqual((text, reserve_calls, primary_calls), ('', 1, 1), answer)


if __name__ == '__main__':
    unittest.main()
