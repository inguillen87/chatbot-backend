import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
from types import SimpleNamespace

from google.cloud import texttospeech

class TestTextToSpeechService(unittest.TestCase):

    @patch('services.google_text_to_speech.texttospeech.TextToSpeechClient')
    def setUp(self, mock_tts_client_class):
        # This mock will be injected into the __init__ of TextToSpeechService
        self.mock_tts_client = mock_tts_client_class.return_value

        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = os.path.join(self.temp_dir.name, "audio_responses")

        # Now instantiate the service in an isolated output directory.
        from services.google_text_to_speech import TextToSpeechService
        self.tts_service = TextToSpeechService(output_dir=self.output_dir)

        # Create a mock response for the synthesize_speech call
        self.mock_audio_content = b'fake-audio-content'
        mock_response = MagicMock()
        mock_response.audio_content = self.mock_audio_content
        self.mock_tts_client.synthesize_speech.return_value = mock_response

        os.makedirs(self.output_dir, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_synthesize_speech_success(self):
        """
        Test successful speech synthesis.
        """
        test_text = "Hello, world!"

        # Call the method to be tested
        result_url = self.tts_service.synthesize_speech(test_text)
        cached_result_url = self.tts_service.synthesize_speech(test_text)

        # 1. Assert that the client was called with the correct parameters
        self.mock_tts_client.synthesize_speech.assert_called_once()
        args, kwargs = self.mock_tts_client.synthesize_speech.call_args

        # Check input text
        synthesis_input = kwargs['input']
        self.assertEqual(synthesis_input.text, test_text)

        # Check voice parameters
        voice_params = kwargs['voice']
        self.assertEqual(voice_params.language_code, 'es-US')
        self.assertEqual(voice_params.name, 'es-US-Standard-A')
        self.assertEqual(voice_params.ssml_gender, texttospeech.SsmlVoiceGender.FEMALE)

        # Check audio config
        audio_config = kwargs['audio_config']
        self.assertEqual(audio_config.audio_encoding, texttospeech.AudioEncoding.MP3)

        # 2. Assert that the result is a valid URL
        self.assertIsNotNone(result_url)
        self.assertEqual(cached_result_url, result_url)
        self.assertIn("/audio_responses/", result_url.replace("\\", "/"))
        self.assertTrue(result_url.endswith(".mp3"))

        # 3. Assert that the audio file was created
        filename = os.path.basename(result_url)
        file_path = os.path.join(self.output_dir, filename)
        self.assertTrue(os.path.exists(file_path))

        # 4. Verify the content of the created file
        with open(file_path, 'rb') as f:
            content = f.read()
        self.assertEqual(content, self.mock_audio_content)

    @patch('services.tts_orchestrator.generar_audio')
    def test_generate_audio_url_uses_shared_cache_without_user_identity(self, mock_generate):
        from services.google_text_to_speech import generate_audio_url

        mock_generate.return_value = "/static/audio_cache/test.mp3"
        user = SimpleNamespace(email="private@example.com", phone="+549260000000")
        rubro = SimpleNamespace(clave="municipio")

        result = generate_audio_url("Menu principal", rubro, user)

        self.assertEqual(result, "/static/audio_cache/test.mp3")
        mock_generate.assert_called_once_with(
            "Menu principal",
            voice=None,
            model=None,
            style=None,
            speed=None,
            cache_namespace="response:municipio:v1",
        )

    def test_synthesize_speech_client_unavailable(self):
        """
        Test behavior when the TTS client is not available.
        """
        self.tts_service.client = None
        result_url = self.tts_service.synthesize_speech("test")
        self.assertIsNone(result_url)

    def test_synthesize_speech_api_error(self):
        """
        Test behavior when the Google API call fails.
        """
        # Configure the mock to raise an exception
        self.mock_tts_client.synthesize_speech.side_effect = Exception("Google API Error")

        test_text = "This will fail."
        result_url = self.tts_service.synthesize_speech(test_text)

        # Assert that the method returns None on failure
        self.assertIsNone(result_url)

if __name__ == '__main__':
    unittest.main()
