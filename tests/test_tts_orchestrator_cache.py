import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.tts_orchestrator import generar_audio


class TestTTSOrchestratorCache(unittest.TestCase):
    def test_reuses_cached_audio_for_same_menu_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                generated = Path("static/audio_responses/generated.mp3")
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(b"audio")

                with patch.dict(os.environ, {"TTS_CACHE_ENABLED": "true"}, clear=False):
                    with patch(
                        "services.openai_tts_bridge.generar_audio_openai",
                        return_value=str(generated),
                    ) as mock_openai_tts:
                        first_url = generar_audio(
                            "Hola. 1. Reclamos. 2. Turnos.",
                            cache_namespace="menu:junin-1:es",
                        )
                        second_url = generar_audio(
                            "Hola. 1. Reclamos. 2. Turnos.",
                            cache_namespace="menu:junin-1:es",
                        )

                self.assertEqual(first_url, second_url)
                self.assertIn("/static/audio_cache/", first_url)
                mock_openai_tts.assert_called_once()
            finally:
                os.chdir(original_cwd)

    def test_cache_can_be_disabled_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                generated = Path("static/audio_responses/generated.mp3")
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(b"audio")

                with patch.dict(os.environ, {"TTS_CACHE_ENABLED": "false"}, clear=False):
                    with patch(
                        "services.openai_tts_bridge.generar_audio_openai",
                        return_value=str(generated),
                    ) as mock_openai_tts:
                        generar_audio("Texto unico de prueba", cache_namespace="diagnostic")
                        generar_audio("Texto unico de prueba", cache_namespace="diagnostic")

                self.assertEqual(mock_openai_tts.call_count, 2)
                self.assertFalse(Path("static/audio_cache").exists())
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
