import os
import unittest
from unittest.mock import MagicMock, patch

from services.vision_fallback_service import analyze_image_smart


class TestVisionFallbackService(unittest.TestCase):
    @patch("services.vision_fallback_service.OpenAI")
    def test_chat_completion_fallback(self, mock_openai):
        from types import SimpleNamespace

        message = SimpleNamespace(
            content='{"labels": ["street"], "objects": ["car"], "text": "hola"}'
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

        self.assertEqual(result["labels"][0]["description"], "street")
        self.assertEqual(result["objects"][0]["name"], "car")

    @patch("services.vision_fallback_service._call_cohere")
    @patch("services.vision_fallback_service._call_openai", return_value=None)
    def test_cohere_fallback(self, mock_openai, mock_cohere):
        mock_cohere.return_value = {"labels": ["road"], "objects": ["pothole"], "text": ""}
        result = analyze_image_smart(b"bytes")
        self.assertEqual(result["objects"][0]["name"], "pothole")


if __name__ == "__main__":
    unittest.main()
