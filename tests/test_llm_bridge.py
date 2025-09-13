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
    @patch('services.llm_bridge.llamar_cohere')
    def test_llamar_llm_fallback_to_cohere(self, mock_cohere, mock_openai):
        clear_llm_cache()
        mock_cohere.return_value = ({'message_body': 'hola cohere'}, {})
        resp, _ = llamar_llm(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola cohere')
        mock_cohere.assert_called_once()


if __name__ == '__main__':
    unittest.main()
