import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.interpretacion_service import interpretacion_service


class InterpretacionAudioTests(unittest.TestCase):
    def test_interpretar_archivo_audio(self):
        archivo = SimpleNamespace(url="http://example.com/audio.ogg", mime="audio/ogg")
        with patch(
            "services.audio_transcription_service.transcribe_audio_from_url",
            return_value="hola mundo",
        ) as transcribe:
            result = interpretacion_service.interpretar_archivo(archivo)
            transcribe.assert_called_once_with(archivo.url, archivo.mime)
            self.assertEqual(result["texto_extraido"], "hola mundo")
            self.assertIsNone(result["datos_estructurados"])


if __name__ == "__main__":
    unittest.main()
