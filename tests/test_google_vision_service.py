import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Ensure the project root is in the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.google_vision_service import GoogleVisionService, analyze_image_from_content, GoogleAPICallError
from google.api_core import exceptions as core_exceptions

class TestGoogleVisionService(unittest.TestCase):
    """Tests for the GoogleVisionService class itself."""

    @patch('services.google_vision_service.vision.ImageAnnotatorClient')
    def test_initialization_success(self, mock_client_constructor):
        """Tests successful initialization of the service client."""
        mock_client_instance = MagicMock()
        mock_client_constructor.return_value = mock_client_instance

        service = GoogleVisionService()

        self.assertIsNotNone(service.client)
        mock_client_constructor.assert_called_once()
        # Test if the ADC check method was called
        mock_client_instance.feature_level_lfp_response_handler.assert_called_once()

    @patch('services.google_vision_service.vision.ImageAnnotatorClient', side_effect=Exception("ADC not found"))
    def test_initialization_failure(self, mock_client_constructor):
        """Tests that the client is None if initialization fails."""
        service = GoogleVisionService()
        self.assertIsNone(service.client)

    @patch('services.google_vision_service.vision.ImageAnnotatorClient')
    def test_analyze_image_success(self, mock_client_constructor):
        """Tests a successful call to the analyze_image method."""
        mock_client = MagicMock()
        mock_client_constructor.return_value = mock_client

        mock_response = MagicMock()
        mock_response.error.message = ""
        mock_client.annotate_image.return_value = mock_response

        service = GoogleVisionService()
        response = service.analyze_image(b'fake_content', [])

        self.assertIsNotNone(response)
        mock_client.annotate_image.assert_called_once()

    def test_analyze_image_no_client(self):
        """Tests that analyze_image raises ConnectionError if the client is not initialized."""
        service = GoogleVisionService()
        service.client = None  # Force client to be None

        with self.assertRaises(ConnectionError):
            service.analyze_image(b'fake_content', [])

    @patch('services.google_vision_service.vision.ImageAnnotatorClient')
    def test_analyze_image_api_error(self, mock_client_constructor):
        """Tests that an API error raises a GoogleAPICallError."""
        mock_client = MagicMock()
        mock_client_constructor.return_value = mock_client

        mock_response = MagicMock()
        mock_response.error.message = "Test API Error"
        mock_client.annotate_image.return_value = mock_response

        service = GoogleVisionService()
        with self.assertRaises(GoogleAPICallError):
            service.analyze_image(b'fake_content', [])

class TestAnalyzeImageFromContentFunction(unittest.TestCase):
    """Tests for the standalone analyze_image_from_content helper function."""

    @patch('services.google_vision_service.GoogleVisionService')
    def test_function_success(self, mock_service_class):
        """Tests a successful analysis by the helper function."""
        mock_service_instance = MagicMock()
        mock_service_class.return_value = mock_service_instance

        mock_response = MagicMock()
        mock_response.label_annotations = [MagicMock(description="car")]
        mock_sedan = MagicMock()
        mock_sedan.name = "sedan"
        mock_response.localized_object_annotations = [mock_sedan]
        mock_response.text_annotations = [MagicMock(description="This is a car")]
        mock_service_instance.analyze_image.return_value = mock_response

        result = analyze_image_from_content(b'fake_content')

        expected = {
            "labels": ["car"],
            "objects": ["sedan"],
            "text": "This is a car"
        }
        self.assertEqual(result, expected)
        mock_service_instance.analyze_image.assert_called_once()

    @patch('services.google_vision_service.GoogleVisionService')
    def test_function_no_client(self, mock_service_class):
        """Tests the helper function when the service client fails to initialize."""
        mock_service_instance = MagicMock()
        mock_service_instance.client = None
        mock_service_class.return_value = mock_service_instance

        result = analyze_image_from_content(b'fake_content')

        self.assertEqual(result, {"error": "El servicio de Vision no está configurado."})

    @patch('services.google_vision_service.GoogleVisionService')
    def test_function_api_call_error(self, mock_service_class):
        """Tests the helper function when the API call itself fails."""
        mock_service_instance = MagicMock()
        mock_service_class.return_value = mock_service_instance
        mock_service_instance.analyze_image.side_effect = core_exceptions.GoogleAPICallError("API unavailable")

        result = analyze_image_from_content(b'fake_content')

        self.assertEqual(result, {"error": "Error interno al procesar la imagen."})

if __name__ == '__main__':
    unittest.main()
