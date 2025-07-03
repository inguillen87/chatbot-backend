import unittest
from unittest.mock import patch, MagicMock

# Suponiendo que google_vision_service.py está en la carpeta 'services'
# y 'tests' está al mismo nivel que 'services'.
# Ajustar la importación si la estructura del proyecto es diferente.
# Para que esto funcione, asegúrate de que el directorio raíz del proyecto esté en PYTHONPATH
# o que estés corriendo las pruebas de una manera que Python pueda encontrar 'services'.
# Una forma común es tener un __init__.py en la raíz y en 'services'.
try:
    from services.google_vision_service import analyze_image_from_content, VISION_CLIENT
except ImportError:
    # Fallback si la importación directa falla (ej. corriendo tests desde una subcarpeta sin setup de path)
    import sys
    import os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    from services.google_vision_service import analyze_image_from_content, VISION_CLIENT

class TestGoogleVisionService(unittest.TestCase):

    @patch('services.google_vision_service.VISION_CLIENT')
    def test_analyze_image_from_content_success(self, mock_vision_client):
        # Configurar el mock para el cliente de Vision
        mock_response = MagicMock()

        # Simular objetos detectados
        mock_object = MagicMock()
        mock_object.name = "Test Object"
        mock_object.score = 0.9
        mock_vertex = MagicMock()
        mock_vertex.x = 0.1
        mock_vertex.y = 0.2
        mock_object.bounding_poly.normalized_vertices = [mock_vertex, mock_vertex, mock_vertex, mock_vertex]

        # Simular etiquetas detectadas
        mock_label = MagicMock()
        mock_label.description = "Test Label"
        mock_label.score = 0.85

        # Simular texto detectado (OCR)
        mock_text_annotation = MagicMock()
        mock_text_annotation.description = "Test OCR Text"
        mock_text_annotation.locale = "es"

        mock_response.localized_object_annotations = [mock_object]
        mock_response.label_annotations = [mock_label]
        mock_response.text_annotations = [mock_text_annotation] # La primera es el texto completo
        mock_response.error.message = "" # Sin error

        mock_vision_client.annotate_image.return_value = mock_response

        # Contenido de imagen de prueba (bytes)
        test_image_content = b"fake_image_bytes"
        min_confidence = 0.5

        expected_result = {
            "objects": [{
                "name": "Test Object",
                "confidence": 0.9,
                "bounding_poly_normalized": [{"x": 0.1, "y": 0.2}] * 4
            }],
            "labels": [{
                "description": "Test Label",
                "confidence": 0.85
            }],
            "text_annotations": [{
                "description": "Test OCR Text",
                "locale": "es"
            }]
        }

        result = analyze_image_from_content(test_image_content, min_confidence)

        self.assertNotIn("error", result)
        self.assertEqual(result["objects"], expected_result["objects"])
        self.assertEqual(result["labels"], expected_result["labels"])
        self.assertEqual(result["text_annotations"], expected_result["text_annotations"])
        mock_vision_client.annotate_image.assert_called_once()

    @patch('services.google_vision_service.VISION_CLIENT')
    def test_analyze_image_api_error(self, mock_vision_client):
        mock_response = MagicMock()
        mock_response.error.message = "API Error Occurred"
        mock_vision_client.annotate_image.return_value = mock_response

        result = analyze_image_from_content(b"fake_image_bytes")
        self.assertIn("error", result)
        self.assertEqual(result["error"], "Vision API error: API Error Occurred")

    def test_analyze_image_no_content(self):
        # No necesita mockear VISION_CLIENT si la validación de contenido es anterior
        result = analyze_image_from_content(b"")
        self.assertIn("error", result)
        self.assertEqual(result["error"], "Contenido de imagen vacío.")

    @patch('services.google_vision_service.VISION_CLIENT', None) # Simular que el cliente no se inicializó
    @patch('services.google_vision_service.logger') # Mockear el logger para verificar mensajes
    def test_analyze_image_no_client(self, mock_logger, mock_vision_client_none):
        # Esta prueba es un poco más compleja porque VISION_CLIENT es global.
        # La forma más simple de probar esto es si la función verifica explícitamente
        # la disponibilidad del cliente al inicio.

        # Para que esta prueba funcione como está, necesitaríamos que la función
        # `analyze_image_from_content` acceda a `services.google_vision_service.VISION_CLIENT`
        # directamente en lugar de tenerlo como un default o inyectado.
        # Asumiendo que `analyze_image_from_content` usa el VISION_CLIENT global:

        # Guardar el estado original del cliente y restaurarlo después
        original_client = VISION_CLIENT # Esto accede al VISION_CLIENT global del módulo
        try:
            # Forzar que el cliente sea None para esta prueba
            # Esto es problemático porque VISION_CLIENT se carga al importar el módulo.
            # Una mejor manera sería inyectar el cliente en la función o usar una clase.
            # Por ahora, si la función `analyze_image_from_content` tiene un check `if not VISION_CLIENT:`,
            # esta prueba funcionaría si pudiéramos setear VISION_CLIENT a None temporalmente.
            # Sin embargo, el @patch ya lo setea a None para el scope de esta prueba.

            result = analyze_image_from_content(b"some_bytes")
            self.assertIn("error", result)
            self.assertEqual(result["error"], "Cliente de Vision no inicializado.")
            # Verificar que se logueó el error (opcional)
            # mock_logger.error.assert_called_with("❌ [VISION_SVC] Cliente de Vision no inicializado. No se puede analizar la imagen.")

        finally:
            # Restaurar el cliente original (esto es complicado con imports globales y mocks a nivel de módulo)
            # En un test real, se buscaría no modificar estados globales o usar fixtures que los manejen.
            # Dado el @patch, VISION_CLIENT se restaura automáticamente después de la prueba.
            pass


if __name__ == '__main__':
    unittest.main()
