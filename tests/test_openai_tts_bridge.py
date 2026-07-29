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
        _, kwargs = mock_client.audio.speech.create.call_args
        self.assertEqual(kwargs['model'], 'gpt-4o-mini-tts')

    @patch('services.openai_tts_bridge.uuid.uuid4', return_value='voice123')
    @patch('services.openai_tts_bridge.os.makedirs')
    @patch('services.openai_tts_bridge.openai.OpenAI')
    @patch('services.openai_tts_bridge.httpx.Client')
    def test_alias_voice_maps_to_supported_option(
        self,
        mock_httpx_client,
        mock_openai,
        mock_makedirs,
        mock_uuid,
    ):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        response = MagicMock()
        mock_client.audio.speech.create.return_value = response
        response.stream_to_file.return_value = None
        mock_httpx_client.return_value = MagicMock()

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}):
            clear_tts_cache()
            generar_audio_openai('hola voz', voice='sol')

        _, kwargs = mock_client.audio.speech.create.call_args
        self.assertEqual(kwargs['voice'], 'shimmer')

    @patch('services.openai_tts_bridge.uuid.uuid4', return_value='style123')
    @patch('services.openai_tts_bridge.os.makedirs')
    @patch('services.openai_tts_bridge.openai.OpenAI')
    @patch('services.openai_tts_bridge.httpx.Client')
    def test_style_uses_current_tts_instructions_field(
        self,
        mock_httpx_client,
        mock_openai,
        _mock_makedirs,
        _mock_uuid,
    ):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.audio.speech.create.return_value = MagicMock()
        mock_httpx_client.return_value = MagicMock()

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}, clear=True):
            clear_tts_cache()
            generar_audio_openai('hola profesional', style='Tono institucional cálido')

        kwargs = mock_client.audio.speech.create.call_args.kwargs
        self.assertEqual(kwargs['instructions'], 'Tono institucional cálido')
        self.assertNotIn('style', kwargs)


    @patch('services.openai_tts_bridge.uuid.uuid4', return_value='voice456')
    @patch('services.openai_tts_bridge.os.makedirs')
    @patch('services.openai_tts_bridge.openai.OpenAI')
    @patch('services.openai_tts_bridge.httpx.Client')
    def test_default_voice_used_when_not_specified(
        self,
        mock_httpx_client,
        mock_openai,
        mock_makedirs,
        mock_uuid,
    ):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        response = MagicMock()
        mock_client.audio.speech.create.return_value = response
        response.stream_to_file.return_value = None
        mock_httpx_client.return_value = MagicMock()

        with patch.dict(
            os.environ,
            {
                'OPENAI_API_KEY': 'test',
                'OPENAI_TTS_DEFAULT_VOICE': 'rioplatense-femenina',
            },
            clear=True,
        ):
            clear_tts_cache()
            generar_audio_openai('hola default')

        _, kwargs = mock_client.audio.speech.create.call_args
        self.assertEqual(kwargs['voice'], 'shimmer')

    @patch('services.openai_tts_bridge.uuid.uuid4', return_value='private-output')
    @patch('services.openai_tts_bridge.os.makedirs')
    @patch('services.openai_tts_bridge.openai.OpenAI')
    @patch('services.openai_tts_bridge.httpx.Client')
    def test_logs_do_not_expose_text_requested_voice_or_output_path(
        self,
        mock_httpx_client,
        mock_openai,
        _mock_makedirs,
        _mock_uuid,
    ):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.audio.speech.create.return_value = MagicMock()
        mock_httpx_client.return_value = MagicMock()
        sensitive = 'DNI 32877851 token=secret C:/private/audio.mp3'

        with (
            patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}, clear=True),
            self.assertLogs('services.openai_tts_bridge', level='INFO') as logs,
        ):
            clear_tts_cache()
            generar_audio_openai(sensitive, voice=sensitive)

        rendered = '\n'.join(logs.output)
        self.assertIn('input_chars=', rendered)
        self.assertNotIn('32877851', rendered)
        self.assertNotIn('token=secret', rendered)
        self.assertNotIn('C:/private', rendered)


if __name__ == '__main__':
    unittest.main()
