import base64
import os
import unittest
from unittest.mock import MagicMock, patch, ANY

from services.vision_fallback_service import analyze_image_smart


class TestVisionFallbackService(unittest.TestCase):
    @patch("services.vision_fallback_service.OpenAI")
    def test_chat_completion_fallback(self, mock_openai):
        from types import SimpleNamespace

        message = SimpleNamespace(
            content='{"labels": ["calle"], "objects": ["auto"], "text": "hola"}'
        )
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=message)]
        mock_chat = MagicMock()
        mock_chat.completions.create.return_value = mock_completion
        mock_client = MagicMock(spec=["chat"])
        mock_client.chat = mock_chat
        mock_openai.return_value = mock_client

        os.environ["OPENAI_API_KEY"] = "test-key"
        result = analyze_image_smart(b"image-bytes")

        self.assertEqual(result["labels"][0]["description"], "calle")
        self.assertEqual(result["objects"][0]["name"], "auto")

    @patch("services.vision_fallback_service.OpenAI")
    def test_openai_extra_text(self, mock_openai):
        from types import SimpleNamespace

        message = SimpleNamespace(
            content='Aquí tienes {"labels": ["calle"], "objects": ["bache"], "text": ""}'
        )
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=message)]
        mock_chat = MagicMock()
        mock_chat.completions.create.return_value = mock_completion
        mock_client = MagicMock(spec=["chat"])
        mock_client.chat = mock_chat
        mock_openai.return_value = mock_client

        os.environ["OPENAI_API_KEY"] = "test-key"
        result = analyze_image_smart(b"img")
        self.assertEqual(result["objects"][0]["name"], "bache")

    @patch("services.vision_fallback_service._call_cohere")
    @patch("services.vision_fallback_service._call_openai", return_value=None)
    def test_cohere_fallback(self, mock_openai, mock_cohere):
        mock_cohere.return_value = {"labels": ["calle"], "objects": ["bache"], "text": ""}
        with patch.dict(os.environ, {"VISION_COHERE_ENABLED": "true"}, clear=False):
            result = analyze_image_smart(b"bytes")
        self.assertEqual(result["objects"][0]["name"], "bache")

    @patch("cohere.Client")
    def test_generate_receives_image_url(self, mock_client_cls):
        from services.vision_fallback_service import _call_cohere

        mock_client = MagicMock()
        mock_client.chat.side_effect = TypeError("no images param")
        gen_resp = MagicMock()
        gen_resp.generations = [MagicMock(text='{"labels": [], "objects": [], "text": ""}')]
        mock_client.generate.return_value = gen_resp
        mock_client_cls.return_value = mock_client

        with patch.dict(
            os.environ,
            {"COHERE_API_KEY": "abc", "VISION_COHERE_ENABLED": "true"},
            clear=False,
        ):
            _call_cohere(b"img-bytes")

        b64 = base64.b64encode(b"img-bytes").decode("utf-8")
        mock_client.generate.assert_called_once_with(
            model="command-r-plus",
            prompt=ANY,
            image_url=f"data:image/jpeg;base64,{b64}",
        )

    @patch("services.vision_fallback_service.OpenAI")
    def test_responses_api_used_when_available(self, mock_openai):
        from types import SimpleNamespace

        output = [SimpleNamespace(content=[SimpleNamespace(text='{"labels": ["bache"], "objects": ["auto"], "text": "peligro"}')])]
        responses_obj = MagicMock()
        responses_obj.create.return_value = SimpleNamespace(output=output)
        mock_client = MagicMock()
        mock_client.responses = responses_obj
        mock_openai.return_value = mock_client

        os.environ["OPENAI_API_KEY"] = "key"
        result = analyze_image_smart(b"img")
        self.assertEqual(result["labels"][0]["description"], "bache")
        self.assertEqual(result["objects"][0]["name"], "auto")

    @patch("services.vision_fallback_service._call_openai", return_value=None)
    @patch("services.vision_fallback_service._call_cohere", return_value=None)
    def test_all_providers_fail(self, mock_cohere, mock_openai):
        result = analyze_image_smart(b"img")
        self.assertEqual(result, {"labels": [], "objects": []})


if __name__ == "__main__":
    unittest.main()
