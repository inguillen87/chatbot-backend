import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.llm_bridge import (
    clear_llm_cache,
    llamar_llm,
    llamar_llm_para_generacion_texto,
)


class TestLLMBridge(unittest.TestCase):
    @patch('services.llm_bridge.llamar_llm_con_fallback')
    def test_llamar_llm_uses_orchestrator(self, mock_orchestrator):
        clear_llm_cache()
        mock_orchestrator.return_value = ({'message_body': 'hola'}, {'provider': 'openai'})
        resp, _ = llamar_llm(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola')
        mock_orchestrator.assert_called_once()
        self.assertEqual(mock_orchestrator.call_args.kwargs['model'], 'gpt-5.6-sol')

    @patch('services.llm_bridge.llamar_llm_con_fallback')
    def test_llamar_llm_uses_cache(self, mock_orchestrator):
        clear_llm_cache()
        mock_orchestrator.return_value = ({'message_body': 'hola'}, {})
        llamar_llm(None, 'hola', {}, [])
        llamar_llm(None, 'hola', {}, [])
        mock_orchestrator.assert_called_once()

    @patch('services.llm_bridge.llamar_llm_con_fallback')
    def test_llamar_llm_returns_controlled_error_when_all_providers_fail(self, mock_orchestrator):
        clear_llm_cache()
        mock_orchestrator.return_value = ({
            'message_body': 'No disponible.',
            'accion_backend': 'error_fatal_llm',
            'datos_estructura': {},
        }, {})
        resp, _ = llamar_llm(None, 'hola', {}, [])

        self.assertEqual(resp['accion_backend'], 'derivar_humano')
        self.assertEqual(resp['datos_estructura']['error_detalle'], 'llm_unavailable')
        self.assertEqual(resp['message_body'], 'No disponible.')

        # Transient provider failures must not poison the one-hour response cache.
        llamar_llm(None, 'hola', {}, [])
        self.assertEqual(mock_orchestrator.call_count, 2)

    @patch('services.llm_bridge.llamar_llm_con_fallback')
    def test_llamar_llm_normalizes_respuesta_usuario(self, mock_orchestrator):
        clear_llm_cache()
        mock_orchestrator.return_value = ({'respuesta_usuario': 'hola gemini'}, {'provider': 'gemini'})
        resp, context = llamar_llm(None, 'hola', {}, [])

        self.assertEqual(resp['message_body'], 'hola gemini')
        self.assertEqual(resp['respuesta_usuario'], 'hola gemini')
        self.assertEqual(context['provider'], 'gemini')

    def test_llamar_llm_para_generacion_texto_uses_responses_once(self):
        class FakeResponses:
            def __init__(self):
                self.calls = 0
                self.kwargs = None

            def create(self, **kwargs):
                self.calls += 1
                self.kwargs = kwargs
                return SimpleNamespace(
                    output_text='{"ok":true}',
                    output=[],
                    status='completed',
                    incomplete_details=None,
                )

        responses = FakeResponses()
        fake_client = SimpleNamespace(responses=responses)
        with patch(
            'services.openai_bridge._get_openai_responses_client',
            return_value=fake_client,
        ):
            result = llamar_llm_para_generacion_texto(
                'Devolve JSON.',
                'hola',
                json_output=True,
            )

        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(responses.calls, 1)
        self.assertEqual(responses.kwargs['model'], 'gpt-5.6-sol')
        self.assertIs(responses.kwargs['store'], False)
        self.assertEqual(responses.kwargs['reasoning'], {'effort': 'none'})
        self.assertEqual(
            responses.kwargs['text']['format'],
            {'type': 'json_object'},
        )

    def test_text_generation_uses_cloudflare_sol_model_when_opted_in(self):
        class FakeResponses:
            def __init__(self):
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    output_text='ok',
                    output=[],
                    status='completed',
                    incomplete_details=None,
                )

        responses = FakeResponses()
        fake_client = SimpleNamespace(responses=responses)
        gateway_env = {
            'CLOUDFLARE_AI_GATEWAY_ENABLED': 'true',
            'CLOUDFLARE_AI_GATEWAY_ACCOUNT_ID': 'c' * 32,
            'CLOUDFLARE_AI_GATEWAY_API_TOKEN': 'cloudflare-text-test-token',
            'CLOUDFLARE_AI_GATEWAY_ID': 'default',
        }
        with (
            patch.dict(os.environ, gateway_env, clear=False),
            patch(
                'services.openai_bridge._get_openai_responses_client',
                return_value=fake_client,
            ),
        ):
            result = llamar_llm_para_generacion_texto(
                'Respondé breve.',
                'hola',
                model='gpt-5.6-terra',
            )

        self.assertEqual(result, 'ok')
        self.assertEqual(responses.kwargs['model'], 'openai/gpt-5.6-sol')
        self.assertEqual(responses.kwargs['reasoning'], {'effort': 'none'})
        self.assertIs(responses.kwargs['store'], False)


if __name__ == '__main__':
    unittest.main()
