import pytest
import unittest
from services.document_processing_service import DocumentProcessingService, document_processing_service
from app import create_app
from config import Config

@pytest.mark.legacy
class TestConfig(Config):
    TESTING = True

@pytest.mark.legacy
class TestDocumentProcessingService(unittest.TestCase):
    def setUp(self):
        # Although the service is simple, we set up a basic app context
        # in case future versions of the service need it.
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_process_document_placeholder_returns_correctly(self):
        """
        Tests that the placeholder 'process_document' method returns the simulated response.
        """
        # Instantiate the service or use the singleton
        service = DocumentProcessingService()

        # Call the method with dummy content
        result = service.process_document(b'some file content', 'application/pdf')

        # Assert the expected placeholder response
        expected_response = {"success": True, "text": "Contenido del documento procesado (simulado)."}
        self.assertEqual(result, expected_response)

    def test_process_document_with_no_content(self):
        """
        Tests that the service handles missing content gracefully.
        """
        service = DocumentProcessingService()
        result = service.process_document(None, 'application/pdf')
        expected_response = {"success": False, "error": "Contenido o tipo de archivo no proporcionado."}
        self.assertEqual(result, expected_response)

if __name__ == '__main__':
    unittest.main()
