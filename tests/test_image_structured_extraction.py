import unittest
from unittest.mock import patch, Mock

from services.interpretacion_service import interpretacion_service


class ImageStructuredExtractionTests(unittest.TestCase):
    def test_image_reclamo_extraction(self):
        with patch("services.interpretacion_service.requests.get") as mock_get, \
             patch("services.interpretacion_service.analyze_image_smart") as mock_vision, \
             patch("services.interpretacion_service.robust_chat") as mock_chat, \
             patch("services.interpretacion_service._clean_llm_json_output", side_effect=lambda x: x):

            mock_resp = Mock()
            mock_resp.content = b"fake-image"
            mock_resp.raise_for_status = lambda: None
            mock_get.return_value = mock_resp

            mock_vision.return_value = {
                "labels": [{"description": "bache"}],
                "objects": [{"name": "pothole"}],
            }

            mock_chat.return_value = (
                '{"categoria": "arreglo de calle", "descripcion_corta_problema": "Bache en la calle",'
                ' "palabras_clave": ["bache", "pothole"]}'
            )

            result = interpretacion_service.interpretar_imagen_para_reclamo(
                "http://example.com/image.jpg", "image/jpeg", user_id=1
            )

            mock_get.assert_called_once()
            mock_vision.assert_called_once()
            mock_chat.assert_called_once()
            self.assertIn("bache", result["palabras_clave"])
            self.assertEqual(result["datos_estructurados"]["categoria"], "arreglo de calle")
            self.assertIn("Bache", result["datos_estructurados"]["descripcion_corta_problema"])


if __name__ == "__main__":
    unittest.main()
