import unittest
from unittest.mock import patch

from services.interpretacion_service import interpretacion_service


class AudioStructuredExtractionTests(unittest.TestCase):
    def test_audio_reclamo_extraction(self):
        with patch("services.audio_transcription_service.transcribe_audio_from_url") as mock_stt, \
             patch("services.interpretacion_service.robust_chat") as mock_chat, \
             patch("services.interpretacion_service._clean_llm_json_output", side_effect=lambda x: x):

            mock_stt.return_value = (
                "Hola, soy Juan Perez, mi correo es juan@example.com,"
                " vivo en Calle Falsa 123. Hay un semáforo roto."
            )

            mock_chat.return_value = (
                '{"tipo_solicitud": "reclamo", "tipo_problema": "semaforo roto",'
                ' "descripcion_corta_problema": "Semáforo roto en la esquina",'
                ' "direccion_problema": "Calle Falsa 123",'
                ' "nombre_ciudadano": "Juan Perez",'
                ' "email_ciudadano": "juan@example.com"}'
            )

            result = interpretacion_service.interpretar_audio_para_reclamo(
                "http://example.com/audio.ogg", "audio/ogg", user_id=42
            )

            mock_stt.assert_called_once_with(
                "http://example.com/audio.ogg", "audio/ogg"
            )
            mock_chat.assert_called_once()
            self.assertEqual(result["datos_estructurados"]["tipo_solicitud"], "reclamo")
            self.assertEqual(result["datos_estructurados"]["email_ciudadano"], "juan@example.com")
            self.assertIn(
                "semáforo",
                result["datos_estructurados"]["descripcion_corta_problema"].lower(),
            )
            self.assertEqual(result["texto_transcrito"][:4], "Hola")


if __name__ == "__main__":
    unittest.main()
