"""Reserve providers: opt-in by key, ordered, and silent about content."""
import os
import unittest
from unittest.mock import patch

from services.llm_reserve_providers import generate_reserve_text, reserve_available

KEYS = ('GROQ_API_KEY', 'GEMINI_API_KEY', 'HF_TOKEN', 'LLM_RESERVE_CUSTOM_API_KEY', 'LLM_RESERVE_CUSTOM_BASE_URL',
        'LLM_RESERVE_CUSTOM_MODEL', 'LLM_RESERVE_PROVIDERS', 'GROQ_RESERVE_MODEL')


class _Reply:
    def __init__(self, status, content=None):
        self.status_code, self._content = status, content

    def json(self):
        return {'choices': [{'message': {'content': self._content}}]}


def _env(**values):
    clean = {k: v for k, v in os.environ.items() if k not in KEYS}
    clean.update(values)
    return patch.dict(os.environ, clean, clear=True)


class ReserveProvidersTest(unittest.TestCase):
    def test_nothing_is_called_without_a_key(self):
        calls = []
        with _env():
            self.assertFalse(reserve_available())
            self.assertEqual(generate_reserve_text('s', 'u', post=lambda *a, **k: calls.append(a)), '')
        self.assertEqual(calls, [])

    def test_next_provider_answers_when_the_first_fails(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return _Reply(429) if 'groq' in url else _Reply(200, ' {"node_ids": []} ')

        with _env(GROQ_API_KEY='k1', HF_TOKEN='k2'):
            self.assertTrue(reserve_available())
            text = generate_reserve_text('sistema', 'pregunta', temperature=0.1, json_output=True, post=post)
        self.assertEqual(text, '{"node_ids": []}')
        self.assertEqual([c[0] for c in calls], ['https://api.groq.com/openai/v1/chat/completions',
                                                 'https://router.huggingface.co/v1/chat/completions'])
        self.assertEqual(calls[0][1]['json']['response_format'], {'type': 'json_object'})
        self.assertNotIn('response_format', calls[1][1]['json'])
        self.assertEqual(calls[1][1]['headers']['Authorization'], 'Bearer k2')
        self.assertIn('JSON', calls[0][1]['json']['messages'][0]['content'])

    def test_order_model_override_and_custom_endpoint(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs['json']['model']))
            raise TimeoutError()

        with _env(GROQ_API_KEY='k', GROQ_RESERVE_MODEL='otro', LLM_RESERVE_PROVIDERS='custom,groq,desconocido',
                  LLM_RESERVE_CUSTOM_API_KEY='c', LLM_RESERVE_CUSTOM_BASE_URL='https://llm.example.test/v1/',
                  LLM_RESERVE_CUSTOM_MODEL='m'):
            self.assertEqual(generate_reserve_text('s', 'u', post=post), '')
        self.assertEqual(calls, [('https://llm.example.test/v1/chat/completions', 'm'),
                                 ('https://api.groq.com/openai/v1/chat/completions', 'otro')])

    def test_a_retired_model_moves_to_the_next_configured_model(self):
        calls = []

        def post(url, **kwargs):
            calls.append(kwargs['json']['model'])
            return _Reply(404) if len(calls) == 1 else _Reply(200, 'ok')

        with _env(GROQ_API_KEY='k', GROQ_RESERVE_MODEL='viejo, nuevo'):
            self.assertEqual(generate_reserve_text('s', 'u', post=post), 'ok')
        self.assertEqual(calls, ['viejo', 'nuevo'])

    def test_custom_endpoint_must_be_https(self):
        with _env(LLM_RESERVE_CUSTOM_API_KEY='c', LLM_RESERVE_CUSTOM_BASE_URL='http://llm.example.test/v1',
                  LLM_RESERVE_CUSTOM_MODEL='m'):
            self.assertFalse(reserve_available())

    def test_logs_never_carry_prompts_or_keys(self):
        with _env(GROQ_API_KEY='clave-secreta'), self.assertLogs('services.llm_reserve_providers', 'INFO') as logs:
            generate_reserve_text('sistema privado', 'pregunta privada', post=lambda *a, **k: _Reply(200, 'ok'))
        joined = ' '.join(logs.output)
        for secret in ('clave-secreta', 'privad', 'ok'):
            self.assertNotIn(secret, joined.replace('outcome', ''))


if __name__ == '__main__':
    unittest.main()
