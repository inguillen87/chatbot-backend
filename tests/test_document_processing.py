import unittest
from unittest.mock import patch, MagicMock
from app import create_app
from services.document_processing_service import DocumentProcessingService
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    GOOGLE_DOCAI_PROCESSOR_ID = "fake-processor-id"

class TestDocumentProcessingService(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    @patch('services.document_processing_service.documentai')
    @patch('services.document_processing_service.get_google_credentials')
    def test_process_document_pdf(self, mock_get_google_credentials, mock_docai):
        # Mocking credentials and client
        mock_get_google_credentials.return_value = 'fake-credentials'
        mock_docai.DocumentProcessorServiceClient.return_value = MagicMock()
        mock_process_request_instance = MagicMock()
        mock_process_request_instance.raw_document.mime_type = 'application/pdf'
        mock_process_request_instance.name = 'fake-processor-id'
        mock_docai.ProcessRequest.return_value = mock_process_request_instance
        mock_docai.RawDocument.return_value = MagicMock()

        # Service instance
        service = DocumentProcessingService()

        # Call the method
        with open('tests/test_files/dummy.pdf', 'rb') as f:
            pdf_content = f.read()

        service.process_document(pdf_content, 'application/pdf')

        # Assertions
        mock_docai.DocumentProcessorServiceClient.return_value.process_document.assert_called_once()
        args, kwargs = mock_docai.DocumentProcessorServiceClient.return_value.process_document.call_args
        request = kwargs['request']
        self.assertEqual(request.raw_document.mime_type, 'application/pdf')
        self.assertIn(self.app.config['GOOGLE_DOCAI_PROCESSOR_ID'], request.name)

if __name__ == '__main__':
    unittest.main()
