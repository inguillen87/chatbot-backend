import unittest
from unittest.mock import patch

from services.gemini_bridge import llamar_gemini, clear_llm_cache


class TestLLMBridge(unittest.TestCase):
    @patch('services.gemini_bridge.llamar_openai')
    def test_llamar_gemini_uses_openai(self, mock_openai):
        clear_llm_cache()
        mock_openai.return_value = ({'message_body': 'hola'}, {})
        resp, _ = llamar_gemini(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola')
        mock_openai.assert_called_once()

    @patch('services.gemini_bridge.llamar_openai')
    def test_llamar_gemini_uses_cache(self, mock_openai):
        clear_llm_cache()
        mock_openai.return_value = ({'message_body': 'hola'}, {})
        llamar_gemini(None, 'hola', {}, [])
        llamar_gemini(None, 'hola', {}, [])
        mock_openai.assert_called_once()

    @patch('services.gemini_bridge.llamar_openai', side_effect=Exception('fail'))
    @patch('services.gemini_bridge.llamar_cohere')
    def test_llamar_gemini_fallback_to_cohere(self, mock_cohere, mock_openai):
        clear_llm_cache()
        mock_cohere.return_value = ({'message_body': 'hola cohere'}, {})
        resp, _ = llamar_gemini(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola cohere')
        mock_cohere.assert_called_once()


if __name__ == '__main__':
    unittest.main()
