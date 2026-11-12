import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from services.openai_tts_bridge import generar_audio_openai, clear_tts_cache


class TestOpenAITTSBridge(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.previous_cwd = os.getcwd()
        os.chdir(self.temp_directory.name)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp_directory.cleanup()
        clear_tts_cache()

    def write_audio(self, path):
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'synthetic-audio-fixture')

    @patch('services.openai_tts_bridge.uuid.uuid4', return_value='abc123')
    @patch('services.openai_tts_bridge.os.makedirs')
    @patch('services.openai_tts_bridge.openai.OpenAI')
    @patch('services.openai_tts_bridge.httpx.Client')
    def test_caches_responses(self, mock_httpx_client, mock_openai, mock_makedirs, mock_uuid):
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        response = MagicMock()
        mock_client.audio.speech.create.return_value = response
        response.stream_to_file.side_effect = self.write_audio
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
        response.stream_to_file.side_effect = self.write_audio
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
        mock_client.audio.speech.create.return_value.stream_to_file.side_effect = self.write_audio
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
        response.stream_to_file.side_effect = self.write_audio
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
        mock_client.audio.speech.create.return_value.stream_to_file.side_effect = self.write_audio
        mock_httpx_client.return_value = MagicMock()
        sensitive = 'PERSONA_FICTICIA_000 token=synthetic-secret C:/synthetic-private/audio.mp3'

        with (
            patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}, clear=True),
            self.assertLogs('services.openai_tts_bridge', level='INFO') as logs,
        ):
            clear_tts_cache()
            generar_audio_openai(sensitive, voice=sensitive)

        rendered = '\n'.join(logs.output)
        self.assertIn('input_chars=', rendered)
        self.assertNotIn('PERSONA_FICTICIA_000', rendered)
        self.assertNotIn('token=synthetic-secret', rendered)
        self.assertNotIn('C:/synthetic-private', rendered)


if __name__ == '__main__':
    unittest.main()
