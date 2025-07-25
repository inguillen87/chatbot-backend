import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.interpretacion_imagen_service import interpretar_imagen_para_chat

class TestImageAnalysis(unittest.TestCase):

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_from_content')
    @patch('services.interpretacion_imagen_service.extract_complaint_details_llm')
    def test_interpretar_imagen_semaforo(self, mock_extract_complaint_details_llm, mock_analyze_image_from_content, mock_descargar_imagen):
        mock_descargar_imagen.return_value = b'dummy_image_content'
        mock_analyze_image_from_content.return_value = {
            "labels": [{"description": "Traffic light", "confidence": 0.9}],
            "objects": [],
            "full_text_annotation": None,
        }
        mock_extract_complaint_details_llm.return_value = {
            "tipo_problema": "Rotura de semaforo",
            "descripcion_problema": "Semáforo roto en la esquina de la calle Falsa y la avenida Siempreviva.",
        }

        archivo_adjunto = {
            "url": "http://example.com/semaforo.jpg",
            "mime_type": "image/jpeg",
        }

        resultado = interpretar_imagen_para_chat(archivo_adjunto, "reclamo_auto_descripcion_categoria")

        self.assertTrue(resultado['es_reclamo'])
        self.assertEqual(resultado['categoria_sugerida'], "rotura de semaforo")
        self.assertIn("Semáforo roto", resultado['descripcion_sugerida'])

if __name__ == '__main__':
    unittest.main()
