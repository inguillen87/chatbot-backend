import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.interpretacion_service import interpretacion_service


class InterpretacionAudioTests(unittest.TestCase):
    def test_interpretar_archivo_audio(self):
        archivo = SimpleNamespace(id=8, url="r2-private://fixture/audio.ogg", mime="audio/ogg")
        with patch("services.attachment_delivery.read_authorized_attachment_bytes", return_value=b'bounded-audio') as reader, patch(
            "services.audio_transcription_service.transcribe_audio_bytes",
            return_value="hola mundo",
        ) as transcribe:
            result = interpretacion_service.interpretar_archivo(archivo)
            reader.assert_called_once_with(archivo.url, archivo.mime, attachment=archivo)
            transcribe.assert_called_once_with(b'bounded-audio', archivo.mime)
            self.assertEqual(result["texto_extraido"], "hola mundo")
            self.assertIsNone(result["datos_estructurados"])

    def test_private_access_denial_never_reaches_transcription(self):
        from services.r2_service import R2ObjectStorageUnavailableError
        archivo = SimpleNamespace(id=8, url='r2-private://fixture/audio.ogg', mime='audio/ogg')
        with patch('services.attachment_delivery.read_authorized_attachment_bytes', side_effect=R2ObjectStorageUnavailableError('private_attachment_access_required')), patch('services.audio_transcription_service.transcribe_audio_bytes') as transcribe:
            result = interpretacion_service.interpretar_archivo(archivo)
        transcribe.assert_not_called()
        self.assertNotIn(archivo.url, str(result))


if __name__ == "__main__":
    unittest.main()
