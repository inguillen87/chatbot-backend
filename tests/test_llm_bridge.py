import os
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

    @patch.dict(os.environ, {"MAX_RETRIES_OPENAI": "1", "BACKOFF_MS": "0"})
    @patch('services.llm_bridge.time.sleep', return_value=None)
    @patch('services.llm_bridge.llamar_openai', side_effect=Exception('fail'))
    @patch('services.llm_bridge.llamar_cohere')
    def test_llamar_llm_fallback_to_cohere(self, mock_cohere, mock_openai, mock_sleep):
        clear_llm_cache()
        mock_cohere.return_value = ({'message_body': 'hola cohere'}, {})
        resp, _ = llamar_llm(None, 'hola', {}, [])
        self.assertEqual(resp['message_body'], 'hola cohere')
        mock_cohere.assert_called_once()
        mock_openai.assert_called_once()

    @patch.dict(os.environ, {"MAX_RETRIES_OPENAI": "1", "BACKOFF_MS": "0"})
    @patch('services.llm_bridge.time.sleep', return_value=None)
    @patch('services.llm_bridge.llamar_openai', side_effect=Exception('fail'))
    @patch('services.llm_bridge.llamar_cohere', return_value=({'message_body': 'hola cohere'}, {}))
    def test_circuit_breaker_skips_openai(self, mock_cohere, mock_openai, mock_sleep):
        clear_llm_cache()
        with patch('services.llm_bridge.time.time', return_value=1000):
            llamar_llm(None, 'hola', {}, [])
        self.assertEqual(mock_openai.call_count, 1)
        with patch('services.llm_bridge.time.time', return_value=1100):
            llamar_llm(None, 'hola2', {}, [])
        self.assertEqual(mock_openai.call_count, 1)
        self.assertEqual(mock_cohere.call_count, 2)


if __name__ == '__main__':
    unittest.main()
