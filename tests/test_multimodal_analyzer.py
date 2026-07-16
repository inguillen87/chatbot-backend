import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.multimodal_analyzer import analizar_imagen_con_fallback, analizar_imagen_openai

class TestMultimodalAnalyzer(unittest.TestCase):

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False)
    @patch('services.multimodal_analyzer.encode_image_to_base64')
    @patch('services.multimodal_analyzer.OpenAI')
    def test_analizar_imagen_openai_success(self, mock_openai_class, mock_encode):
        # Arrange
        mock_encode.return_value = "fake_base64_string"

        mock_openai_instance = MagicMock()
        mock_completion = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = '{"intent": "crear_reclamo", "data": {"categoria": "Arreglo de calle"}}'
        mock_choice.message = mock_message
        mock_completion.choices = [mock_choice]
        mock_openai_instance.chat.completions.create.return_value = mock_completion
        mock_openai_class.return_value = mock_openai_instance

        test_image = "path/to/fake/image.jpg"
        test_prompt = "test prompt"

        # Act
        result = analizar_imagen_openai(test_image, test_prompt)

        # Assert
        self.assertIsNotNone(result)
        self.assertEqual(result["intent"], "crear_reclamo")
        self.assertEqual(result["data"]["categoria"], "Arreglo de calle")
        mock_encode.assert_called_once_with(test_image)
        mock_openai_class.assert_called_once_with(api_key="test-openai-key")
        mock_openai_instance.chat.completions.create.assert_called_once()

    @patch('services.multimodal_analyzer.analizar_imagen_openai')
    @patch('logging.Logger.warning')
    def test_fallback_to_legacy_placeholder(self, mock_log_warning, mock_analizar_openai):
        # Arrange
        mock_analizar_openai.return_value = None
        test_image = "path/to/fake/image.jpg"
        test_prompt = "test prompt"

        # Act
        result = analizar_imagen_con_fallback(test_image, test_prompt)

        # Assert
        self.assertIsNone(result)
        mock_analizar_openai.assert_called_once_with(test_image, test_prompt)
        mock_log_warning.assert_called_with("Falling back to placeholder/legacy logic...")

if __name__ == '__main__':
    unittest.main()
