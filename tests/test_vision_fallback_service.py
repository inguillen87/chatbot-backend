import base64
import os
import unittest
from unittest.mock import MagicMock, patch, ANY

from services.vision_fallback_service import analyze_image_smart


class TestVisionFallbackService(unittest.TestCase):


    @patch("services.vision_fallback_service._call_openai", return_value=None)
    @patch("services.vision_fallback_service._call_cohere", return_value=None)
    def test_all_providers_fail(self, mock_cohere, mock_openai):
        result = analyze_image_smart(b"img")
        self.assertEqual(result, {"labels": [], "objects": []})


if __name__ == "__main__":
    unittest.main()
