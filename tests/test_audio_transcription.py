import unittest
from unittest.mock import MagicMock, patch

from services.audio_transcription_service import (
    audio_translation_capabilities,
    normalize_spanish_transcription,
    resolve_transcription_language,
    transcribe_audio_from_url,
)


class TestAudioTranscriptionService(unittest.TestCase):

    @patch("services.audio_transcription_service.requests.get")
    @patch("services.audio_transcription_service.openai_client")
    def test_transcribe_audio_from_url_success(self, mock_openai_client, mock_requests_get):
        mock_audio_content = b"fake_audio_content"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = mock_audio_content
        mock_requests_get.return_value = mock_response

        mock_transcriptions = mock_openai_client.audio.transcriptions
        mock_create = mock_transcriptions.create
        mock_result = MagicMock()
        mock_result.text = "hello world"
        mock_create.return_value = mock_result

        result = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertEqual(result, "hello world")
        mock_requests_get.assert_called_once_with("http://example.com/audio.ogg", auth=("fake_sid", "fake_token"))
        mock_create.assert_called_once()

    @patch("services.audio_transcription_service.requests.get")
    def test_transcribe_audio_from_url_download_fails(self, mock_requests_get):
        mock_requests_get.side_effect = Exception("Download failed")

        result = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertIsNone(result)

    def test_normalize_spanish_transcription(self):
        raw = "sr juan xq dnd estan uds"
        normalized = normalize_spanish_transcription(raw)

        self.assertEqual(normalized, "señor juan porque donde estan ustedes")

    def test_audio_translation_capabilities_default_to_language_detection(self):
        with patch.dict("os.environ", {"OPENAI_STT_LANGUAGE": "auto"}, clear=False):
            self.assertIsNone(resolve_transcription_language())
            capabilities = audio_translation_capabilities()

        self.assertTrue(capabilities["language_detection"])
        self.assertEqual(capabilities["supported_languages"], ["es", "en", "pt"])
        self.assertEqual(capabilities["target_language"], "es")

    @patch("services.audio_transcription_service.requests.get")
    @patch("services.audio_transcription_service.openai_client")
    def test_openai_transcription_omits_language_when_auto_detecting(self, mock_openai_client, mock_requests_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b"fake_audio_content"
        mock_requests_get.return_value = mock_response
        mock_result = MagicMock()
        mock_result.text = "hello world"
        mock_openai_client.audio.transcriptions.create.return_value = mock_result

        with patch.dict("os.environ", {"OPENAI_STT_LANGUAGE": "auto", "STT_PROVIDER_ORDER": "openai"}, clear=False):
            result = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertEqual(result, "hello world")
        create_kwargs = mock_openai_client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(create_kwargs["model"], "gpt-4o-mini-transcribe")
        self.assertNotIn("language", create_kwargs)

    @patch("services.audio_transcription_service.requests.get")
    @patch("services.audio_transcription_service.openai_client")
    def test_openai_transcription_normalizes_spanish_when_forced(self, mock_openai_client, mock_requests_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b"fake_audio_content"
        mock_requests_get.return_value = mock_response
        mock_result = MagicMock()
        mock_result.text = "sr juan xq"
        mock_openai_client.audio.transcriptions.create.return_value = mock_result

        with patch.dict("os.environ", {"OPENAI_STT_LANGUAGE": "es", "STT_PROVIDER_ORDER": "openai"}, clear=False):
            result = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertEqual(result, "señor juan porque")
        create_kwargs = mock_openai_client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(create_kwargs["language"], "es")

    @patch("services.audio_transcription_service.requests.get")
    @patch("services.audio_transcription_service.openai_client")
    def test_transcribe_audio_from_url_transcription_fails(self, mock_openai_client, mock_requests_get):
        mock_audio_content = b"fake_audio_content"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = mock_audio_content
        mock_requests_get.return_value = mock_response

        mock_openai_client.audio.transcriptions.create.side_effect = Exception("Transcription failed")

        with patch.dict("os.environ", {"STT_PROVIDER_ORDER": "openai"}, clear=False):
            result = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
