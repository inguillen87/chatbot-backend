import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from flask import Flask

from services import vision_fallback_service as vision_service


class TestVisionFallbackService(unittest.TestCase):
    def setUp(self):
        self.network_patcher = patch.dict(
            os.environ,
            {
                "OPENAI_ALLOW_NETWORK_IN_TESTS": "1",
                "COHERE_ALLOW_NETWORK_IN_TESTS": "1",
            },
            clear=False,
        )
        self.network_patcher.start()
        vision_service._OPENAI_CLIENT = None
        vision_service._OPENAI_CLIENT_KEY_DIGEST = None

    def tearDown(self):
        vision_service._OPENAI_CLIENT = None
        vision_service._OPENAI_CLIENT_KEY_DIGEST = None
        self.network_patcher.stop()

    def test_responses_api_uses_documented_image_and_structured_output_shape(self):
        response = SimpleNamespace(
            output_text='{"labels": ["bache"], "objects": ["auto"], "text": "peligro"}',
            output=[],
        )
        client = MagicMock()
        client.responses.create.return_value = response

        with patch.object(vision_service, "_get_openai_client", return_value=client):
            result = vision_service.analyze_image_smart(b"\x89PNG\r\n\x1a\nimage")

        self.assertEqual(result["labels"][0]["description"], "bache")
        self.assertEqual(result["objects"][0]["name"], "auto")
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], vision_service.DEFAULT_OPENAI_VISION_MODEL)
        self.assertFalse(request["store"])
        self.assertNotIn("temperature", request)
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])
        image_part = request["input"][0]["content"][1]
        self.assertEqual(image_part["type"], "input_image")
        self.assertEqual(image_part["detail"], "auto")
        self.assertTrue(image_part["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("image", image_part)
        self.assertFalse(client.chat.completions.create.called)

    def test_ambiguous_responses_failure_is_not_retried_or_sent_to_fallback_provider(self):
        sensitive = "DNI 32877851 https://private.example.test?token=secret"
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError(sensitive)

        with (
            patch.object(vision_service, "_get_openai_client", return_value=client),
            patch.object(vision_service, "_call_cohere") as cohere,
            patch.dict(os.environ, {"VISION_COHERE_ENABLED": "true"}, clear=False),
            self.assertLogs("services.vision_fallback_service", level="WARNING") as logs,
        ):
            result = vision_service.analyze_image_smart(b"image")

        self.assertEqual(result, {"labels": [], "objects": []})
        client.responses.create.assert_called_once()
        self.assertFalse(client.chat.completions.create.called)
        cohere.assert_not_called()
        rendered = "\n".join(logs.output)
        self.assertIn("error_type=RuntimeError", rendered)
        self.assertIn("fallback_suppressed=true", rendered)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn("32877851", rendered)

    def test_lazy_client_uses_flask_config_and_disables_sdk_retries(self):
        app = Flask(__name__)
        app.config["OPENAI_API_KEY"] = "app-key"
        app.config["OPENAI_VISION_TIMEOUT_SECONDS"] = "17"

        with (
            app.app_context(),
            patch("services.vision_fallback_service.httpx.Client") as http_client,
            patch("services.vision_fallback_service.OpenAI") as openai,
        ):
            first = vision_service._get_openai_client()
            second = vision_service._get_openai_client()

        self.assertIs(first, second)
        openai.assert_called_once_with(
            api_key="app-key",
            http_client=http_client.return_value,
            max_retries=0,
            timeout=17.0,
        )

    def test_test_network_policy_blocks_before_openai_constructor(self):
        with (
            patch.dict(
                os.environ,
                {"TESTING": "1", "OPENAI_API_KEY": "private-test-key"},
                clear=True,
            ),
            patch("services.vision_fallback_service.OpenAI") as constructor,
            self.assertRaisesRegex(
                vision_service.OpenAIConfigurationError,
                "openai_test_network_disabled",
            ),
        ):
            vision_service._get_openai_client()

        constructor.assert_not_called()

    def test_json_parse_warning_does_not_log_provider_output(self):
        sensitive = "not-json DNI 32877851 token=secret"
        with self.assertLogs("services.vision_fallback_service", level="WARNING") as logs:
            result = vision_service._safe_json_loads(sensitive)

        self.assertEqual(result, {})
        rendered = "\n".join(logs.output)
        self.assertIn(f"response_chars={len(sensitive)}", rendered)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn("32877851", rendered)

    @patch("services.vision_fallback_service._call_cohere")
    @patch("services.vision_fallback_service._call_openai", return_value=None)
    def test_explicit_cohere_fallback_remains_available_before_openai_send(
        self,
        _openai,
        cohere,
    ):
        cohere.return_value = {"labels": ["calle"], "objects": ["bache"], "text": ""}
        with patch.dict(os.environ, {"VISION_COHERE_ENABLED": "true"}, clear=False):
            result = vision_service.analyze_image_smart(b"bytes")

        self.assertEqual(result["objects"][0]["name"], "bache")

    @patch("cohere.Client")
    def test_generate_receives_image_url_for_pre_request_sdk_type_fallback(self, client_cls):
        client = MagicMock()
        client.chat.side_effect = TypeError("no images param")
        client.generate.return_value = MagicMock(
            generations=[MagicMock(text='{"labels": [], "objects": [], "text": ""}')]
        )
        client_cls.return_value = client

        with patch.dict(
            os.environ,
            {"COHERE_API_KEY": "abc", "VISION_COHERE_ENABLED": "true"},
            clear=False,
        ):
            vision_service._call_cohere(b"img-bytes")

        encoded = base64.b64encode(b"img-bytes").decode("utf-8")
        client.generate.assert_called_once_with(
            model="command-r-plus",
            prompt=ANY,
            image_url=f"data:image/jpeg;base64,{encoded}",
        )

    @patch("services.vision_fallback_service._call_openai", return_value=None)
    @patch("services.vision_fallback_service._call_cohere", return_value=None)
    def test_all_providers_fail(self, _cohere, _openai):
        result = vision_service.analyze_image_smart(b"img")
        self.assertEqual(result, {"labels": [], "objects": []})


if __name__ == "__main__":
    unittest.main()
