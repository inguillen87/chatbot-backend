import unittest
from unittest.mock import patch, MagicMock
from services.image_processing_service import ImageProcessingService

class TestImageProcessingService(unittest.TestCase):

    @patch('services.image_processing_service.vision.ImageAnnotatorClient')
    def test_analyze_image_success(self, mock_vision_client):
        # Mock de la respuesta de la API de Google Cloud Vision
        mock_response = MagicMock()
        mock_response.error.message = ''
        mock_response.label_annotations = [MagicMock(description='test label')]
        mock_response.text_annotations = [MagicMock(description='test text')]
        mock_response.localized_object_annotations = [MagicMock(name='test object')]

        mock_vision_client.return_value.annotate_image.return_value = mock_response

        service = ImageProcessingService()
        result = service.analyze_image(b'fake_image_content')

        self.assertIn('labels', result)
        self.assertIn('texts', result)
        self.assertIn('objects', result)
        self.assertEqual(result['labels'], ['test label'])
        self.assertEqual(result['texts'], ['test text'])
        self.assertEqual(result['objects'][0].name, 'test object')

    @patch('services.image_processing_service.vision.ImageAnnotatorClient')
    def test_analyze_image_api_error(self, mock_vision_client):
        # Mock de un error en la API de Google Cloud Vision
        mock_response = MagicMock()
        mock_response.error.message = 'API error'

        mock_vision_client.return_value.annotate_image.return_value = mock_response

        service = ImageProcessingService()
        result = service.analyze_image(b'fake_image_content')

        self.assertIn('error', result)
        self.assertEqual(result['error'], 'API error')

    @patch('services.image_processing_service.vision.ImageAnnotatorClient')
    def test_analyze_image_exception(self, mock_vision_client):
        # Mock de una excepción durante la llamada a la API
        mock_vision_client.return_value.annotate_image.side_effect = Exception('Test exception')

        service = ImageProcessingService()
        result = service.analyze_image(b'fake_image_content')

        self.assertIn('error', result)
        self.assertEqual(result['error'], 'Error inesperado al procesar la imagen.')

if __name__ == '__main__':
    unittest.main()
