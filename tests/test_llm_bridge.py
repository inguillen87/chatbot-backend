import unittest
from unittest.mock import patch

from services.llm_bridge import llamar_llm, clear_llm_cache


class TestLLMBridge(unittest.TestCase):
    @patch('services.llm_bridge.llamar_llm_con_fallback')
    def test_llamar_llm_uses_orchestrator(self, mock_orchestrator):
        clear_llm_cache()
        mock_orchestrator.return_value = ({'message_body': 'hola'}, {'provider': 'openai'})
        resp, _ = llamar_llm(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola')
        mock_orchestrator.assert_called_once()

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

    @patch('services.llm_bridge.llamar_llm_con_fallback')
    def test_llamar_llm_normalizes_respuesta_usuario(self, mock_orchestrator):
        clear_llm_cache()
        mock_orchestrator.return_value = ({'respuesta_usuario': 'hola gemini'}, {'provider': 'gemini'})
        resp, context = llamar_llm(None, 'hola', {}, [])

        self.assertEqual(resp['message_body'], 'hola gemini')
        self.assertEqual(resp['respuesta_usuario'], 'hola gemini')
        self.assertEqual(context['provider'], 'gemini')


if __name__ == '__main__':
    unittest.main()
