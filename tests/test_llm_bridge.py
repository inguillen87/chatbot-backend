import unittest
from unittest.mock import patch

from services.llm_bridge import llamar_llm, clear_llm_cache


class TestLLMBridge(unittest.TestCase):
    @patch('services.llm_bridge.llamar_openai')
    def test_llamar_llm_uses_openai(self, mock_openai):
        clear_llm_cache()
        mock_openai.return_value = ({'message_body': 'hola'}, {})
        resp, _ = llamar_llm(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola')
        mock_openai.assert_called_once()

    @patch('services.llm_bridge.llamar_openai')
    def test_llamar_llm_uses_cache(self, mock_openai):
        clear_llm_cache()
        mock_openai.return_value = ({'message_body': 'hola'}, {})
        llamar_llm(None, 'hola', {}, [])
        llamar_llm(None, 'hola', {}, [])
        mock_openai.assert_called_once()

    @patch('services.llm_bridge.llamar_openai', side_effect=Exception('fail'))
    def test_llamar_llm_returns_controlled_error_when_cohere_disabled(self, mock_openai):
        clear_llm_cache()
        with patch.dict('os.environ', {'LLM_COHERE_ENABLED': 'false', 'COHERE_ENABLED': 'false'}, clear=False):
            resp, _ = llamar_llm(None, 'hola', {}, [])

        self.assertEqual(resp['accion_backend'], 'derivar_humano')
        self.assertEqual(resp['datos_estructura']['error_detalle'], 'openai_unavailable')

    @patch('services.llm_bridge.llamar_openai', side_effect=Exception('fail'))
    @patch('services.cohere_bridge.llamar_cohere')
    def test_llamar_llm_uses_explicit_cohere_when_enabled(self, mock_cohere, mock_openai):
        clear_llm_cache()
        mock_cohere.return_value = ({'message_body': 'hola cohere'}, {})
        with patch.dict('os.environ', {'LLM_COHERE_ENABLED': 'true'}, clear=False):
            resp, _ = llamar_llm(None, 'hola', {}, [])

        self.assertEqual(resp['message_body'], 'hola cohere')
        mock_cohere.assert_called_once()


if __name__ == '__main__':
    unittest.main()
