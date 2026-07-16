import unittest
from unittest.mock import patch
from services.document_processing_service import DocumentProcessingService, document_processing_service
from app import create_app
from config import TestConfig

class TestDocumentProcessingService(unittest.TestCase):
    def setUp(self):
        # Although the service is simple, we set up a basic app context
        # in case future versions of the service need it.
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_process_document_rejects_malformed_pdf_gracefully(self):
        service = DocumentProcessingService()
        result = service.process_document(b'some file content', 'application/pdf')
        self.assertFalse(result["success"])
        self.assertIn("no se pudo procesar", result["error"].lower())

    @patch.object(
        DocumentProcessingService,
        "_build_structured_response",
        return_value={"resumen": "Pedido detectado", "items": []},
    )
    def test_process_text_document_returns_structured_payload(self, _mock_build):
        service = DocumentProcessingService()
        result = service.process_document(
            b"10 cajas de tornillos",
            "text/plain",
            "pedido.txt",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["texto_extraido"], "10 cajas de tornillos")
        self.assertEqual(result["datos_estructurados"]["resumen"], "Pedido detectado")

    def test_process_document_with_no_content(self):
        """
        Tests that the service handles missing content gracefully.
        """
        service = DocumentProcessingService()
        result = service.process_document(None, 'application/pdf')
        expected_response = {"success": False, "error": "Contenido vacío."}
        self.assertEqual(result, expected_response)

if __name__ == '__main__':
    unittest.main()
