import os
import unittest
from unittest.mock import patch, MagicMock

from services.openai_tts_bridge import generar_audio_openai, clear_tts_cache


class TestOpenAITTSBridge(unittest.TestCase):
    @patch('services.openai_tts_bridge.uuid.uuid4', return_value='abc123')
    @patch('services.openai_tts_bridge.os.makedirs')
    @patch('services.openai_tts_bridge.openai.OpenAI')
    @patch('services.openai_tts_bridge.httpx.Client')
    def test_caches_responses(self, mock_httpx_client, mock_openai, mock_makedirs, mock_uuid):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        response = MagicMock()
        mock_client.audio.speech.create.return_value = response
        response.stream_to_file.return_value = None
        mock_httpx_client.return_value = MagicMock()

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}):
            clear_tts_cache()
            path1 = generar_audio_openai('hola mundo')
            path2 = generar_audio_openai('hola mundo')

        self.assertEqual(path1, path2)
        mock_client.audio.speech.create.assert_called_once()


if __name__ == '__main__':
    unittest.main()
