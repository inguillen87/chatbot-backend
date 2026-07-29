import os
import unittest
from unittest.mock import MagicMock, patch

import requests
from flask import Flask

import services.audio_transcription_service as audio_service
from services.audio_transcription_service import (
    OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES,
    audio_translation_capabilities,
    clear_transcription_cache,
    normalize_spanish_transcription,
    resolve_transcription_language,
    transcribe_audio_bytes,
    transcribe_audio_from_url,
)


class TestAudioTranscriptionService(unittest.TestCase):
    def setUp(self):
        clear_transcription_cache()

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
        mock_requests_get.assert_called_once_with(
            "http://example.com/audio.ogg",
            auth=("fake_sid", "fake_token"),
            timeout=12.0,
        )
        mock_create.assert_called_once()

    @patch("services.audio_transcription_service.requests.get")
    def test_transcribe_audio_from_url_download_fails(self, mock_requests_get):
        mock_requests_get.side_effect = Exception("Download failed")

        result = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertIsNone(result)

    @patch("services.audio_transcription_service.requests.get")
    def test_download_error_log_does_not_include_url_token_or_exception_body(self, mock_requests_get):
        sensitive = "https://media.example.test/private?token=secret DNI=32877851"
        mock_requests_get.side_effect = requests.exceptions.RequestException(sensitive)

        with self.assertLogs("services.audio_transcription_service", level="WARNING") as logs:
            result = transcribe_audio_from_url(
                sensitive,
                "audio/ogg; codecs=opus",
                "AC-sensitive",
                "auth-sensitive",
            )

        rendered_logs = "\n".join(logs.output)
        self.assertIsNone(result)
        self.assertIn("error_type=RequestException", rendered_logs)
        self.assertNotIn(sensitive, rendered_logs)
        self.assertNotIn("auth-sensitive", rendered_logs)
        self.assertNotIn("32877851", rendered_logs)

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
        self.assertEqual(
            capabilities["max_audio_bytes"],
            OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES,
        )
        self.assertTrue(capabilities["cache"]["enabled"])
        self.assertEqual(capabilities["providers"], ["openai"])

    def test_openai_client_is_lazy_and_does_not_use_dummy_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(audio_service, "openai_client", None),
            patch.object(audio_service, "http_client", None),
            patch("services.audio_transcription_service.OpenAI") as mock_constructor,
            self.assertLogs("services.audio_transcription_service", level="WARNING") as logs,
        ):
            result = transcribe_audio_bytes(b"audio", "audio/webm")

        self.assertIsNone(result)
        mock_constructor.assert_not_called()
        self.assertIn("reason=missing_api_key", "\n".join(logs.output))

    def test_lazy_client_prefers_explicit_flask_app_config(self):
        app = Flask(__name__)
        app.config["OPENAI_API_KEY"] = "app-config-key"
        mock_result = MagicMock(text="hola")

        with (
            app.app_context(),
            patch.dict(os.environ, {"OPENAI_API_KEY": "environment-key"}, clear=False),
            patch.object(audio_service, "openai_client", None),
            patch.object(audio_service, "http_client", None),
            patch("services.audio_transcription_service.httpx.Client") as mock_httpx,
            patch("services.audio_transcription_service.OpenAI") as mock_constructor,
        ):
            mock_constructor.return_value.audio.transcriptions.create.return_value = mock_result
            result = transcribe_audio_bytes(b"audio", "audio/webm")

        self.assertEqual(result, "hola")
        mock_constructor.assert_called_once_with(
            api_key="app-config-key",
            http_client=mock_httpx.return_value,
        )

    def test_lazy_client_reads_environment_at_first_use(self):
        mock_result = MagicMock(text="hola desde dotenv")

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "late-loaded-key"}, clear=False),
            patch.object(audio_service, "openai_client", None),
            patch.object(audio_service, "http_client", None),
            patch("services.audio_transcription_service.httpx.Client") as mock_httpx,
            patch("services.audio_transcription_service.OpenAI") as mock_constructor,
        ):
            mock_constructor.return_value.audio.transcriptions.create.return_value = mock_result
            result = transcribe_audio_bytes(b"audio", "audio/webm")

        self.assertEqual(result, "hola desde dotenv")
        mock_constructor.assert_called_once_with(
            api_key="late-loaded-key",
            http_client=mock_httpx.return_value,
        )

    @patch("services.audio_transcription_service.openai_client")
    def test_mime_parameters_are_removed_from_whatsapp_ogg_filename(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="audio ok")

        result = transcribe_audio_bytes(
            b"ogg-opus-audio",
            "Audio/OGG; codecs=opus",
        )

        self.assertEqual(result, "audio ok")
        file_arg = mock_openai_client.audio.transcriptions.create.call_args.kwargs["file"]
        self.assertEqual(file_arg.name, "audio.ogg")

    @patch("services.audio_transcription_service.openai_client")
    def test_configured_audio_limit_rejects_bytes_before_provider_call(self, mock_openai_client):
        with patch.dict(os.environ, {"STT_MAX_AUDIO_BYTES": "4"}, clear=False):
            with self.assertLogs("services.audio_transcription_service", level="WARNING") as logs:
                result = transcribe_audio_bytes(b"12345", "audio/webm")

        self.assertIsNone(result)
        mock_openai_client.audio.transcriptions.create.assert_not_called()
        self.assertIn("reason=file_too_large", "\n".join(logs.output))

    def test_audio_limit_cannot_exceed_openai_upload_contract(self):
        with patch.dict(
            os.environ,
            {"STT_MAX_AUDIO_BYTES": str(OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES * 2)},
            clear=False,
        ):
            capabilities = audio_translation_capabilities()

        self.assertEqual(
            capabilities["max_audio_bytes"],
            OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES,
        )

    @patch("services.audio_transcription_service.requests.get")
    @patch("services.audio_transcription_service.openai_client")
    def test_content_length_limit_rejects_before_provider_call(
        self,
        mock_openai_client,
        mock_requests_get,
    ):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {"Content-Length": "5"}
        mock_response.content = b"12345"
        mock_requests_get.return_value = mock_response

        with patch.dict(os.environ, {"STT_MAX_AUDIO_BYTES": "4"}, clear=False):
            result = transcribe_audio_from_url(
                "https://media.example.test/private",
                "audio/webm",
                "fake_sid",
                "fake_token",
            )

        self.assertIsNone(result)
        mock_openai_client.audio.transcriptions.create.assert_not_called()

    @patch("services.audio_transcription_service.openai_client")
    def test_provider_error_log_does_not_include_exception_body_or_pii(self, mock_openai_client):
        sensitive = "https://media.example.test?token=secret DNI=32877851"
        mock_openai_client.audio.transcriptions.create.side_effect = RuntimeError(sensitive)

        with (
            patch.dict(os.environ, {"STT_PROVIDER_ORDER": "openai"}, clear=False),
            self.assertLogs("services.audio_transcription_service", level="WARNING") as logs,
        ):
            result = transcribe_audio_bytes(b"audio", "audio/webm")

        rendered_logs = "\n".join(logs.output)
        self.assertIsNone(result)
        self.assertIn("error_type=RuntimeError", rendered_logs)
        self.assertNotIn(sensitive, rendered_logs)
        self.assertNotIn("32877851", rendered_logs)

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
        self.assertEqual(create_kwargs["model"], "gpt-transcribe")
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

    @patch("services.audio_transcription_service.requests.get")
    @patch("services.audio_transcription_service.openai_client")
    def test_transcribe_audio_from_url_uses_cache_for_repeated_audio(self, mock_openai_client, mock_requests_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b"same_audio_content"
        mock_requests_get.return_value = mock_response

        mock_result = MagicMock()
        mock_result.text = "hola cache"
        mock_openai_client.audio.transcriptions.create.return_value = mock_result

        with patch.dict("os.environ", {"OPENAI_STT_LANGUAGE": "auto", "STT_PROVIDER_ORDER": "openai"}, clear=False):
            first = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")
            second = transcribe_audio_from_url("http://example.com/audio.ogg", "audio/ogg", "fake_sid", "fake_token")

        self.assertEqual(first, "hola cache")
        self.assertEqual(second, "hola cache")
        mock_requests_get.assert_called_once()
        mock_openai_client.audio.transcriptions.create.assert_called_once()

    @patch("services.audio_transcription_service.openai_client")
    def test_transcribe_audio_bytes_uses_content_cache_without_download(self, mock_openai_client):
        mock_result = MagicMock()
        mock_result.text = "menu accesible cacheado"
        mock_openai_client.audio.transcriptions.create.return_value = mock_result

        with patch.dict("os.environ", {"OPENAI_STT_LANGUAGE": "auto", "STT_PROVIDER_ORDER": "openai"}, clear=False):
            first = transcribe_audio_bytes(b"same_menu_audio", "audio/ogg", cache_url="twilio://menu/main")
            second = transcribe_audio_bytes(b"same_menu_audio", "audio/ogg", cache_url="twilio://menu/main")

        self.assertEqual(first, "menu accesible cacheado")
        self.assertEqual(second, "menu accesible cacheado")
        mock_openai_client.audio.transcriptions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
