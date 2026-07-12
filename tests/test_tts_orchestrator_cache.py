import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from services.tts_orchestrator import (
    generar_audio,
    get_tts_audio_cache_public_config,
    get_tts_cache_metrics,
    reset_tts_cache_metrics,
    warm_tts_cache,
)


class TestTTSOrchestratorCache(unittest.TestCase):
    def setUp(self):
        reset_tts_cache_metrics()

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
                metrics = get_tts_cache_metrics()
                self.assertEqual(metrics["cache_misses"], 1)
                self.assertEqual(metrics["cache_hits"], 1)
                self.assertEqual(metrics["cache_writes"], 1)
            finally:
                os.chdir(original_cwd)

    def test_concurrent_fixed_menu_requests_generate_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                generated = Path("static/audio_responses/generated.mp3")
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(b"audio")

                def delayed_provider(_text, **_kwargs):
                    time.sleep(0.05)
                    return str(generated)

                with patch.dict(os.environ, {"TTS_CACHE_ENABLED": "true"}, clear=False):
                    with patch(
                        "services.openai_tts_bridge.generar_audio_openai",
                        side_effect=delayed_provider,
                    ) as mock_openai_tts:
                        with ThreadPoolExecutor(max_workers=4) as executor:
                            urls = list(
                                executor.map(
                                    lambda _index: generar_audio(
                                        "Menu accesible estable. Opcion 1, Reclamos.",
                                        cache_namespace="menu:junin:accessible:v1",
                                    ),
                                    range(4),
                                )
                            )

                self.assertEqual(len(set(urls)), 1)
                self.assertIn("/static/audio_cache/", urls[0])
                self.assertEqual(mock_openai_tts.call_count, 1)
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
                metrics = get_tts_cache_metrics()
                self.assertEqual(metrics["cache_disabled"], 2)
                self.assertEqual(metrics["cache_hits"], 0)
            finally:
                os.chdir(original_cwd)

    def test_cached_fixed_menu_audio_can_use_cdn_public_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                generated = Path("static/audio_responses/generated.mp3")
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(b"audio")

                with patch.dict(
                    os.environ,
                    {
                        "TTS_CACHE_ENABLED": "true",
                        "TTS_AUDIO_CACHE_PUBLIC_BASE_URL": "https://cdn.chatboc.ar/audio",
                        "CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL": "",
                    },
                    clear=False,
                ):
                    with patch(
                        "services.openai_tts_bridge.generar_audio_openai",
                        return_value=str(generated),
                    ) as mock_openai_tts:
                        first_url = generar_audio(
                            "Menu principal. Opcion 1, Reclamos. Opcion 2, Turnos.",
                            cache_namespace="whatsapp:menu:junin:main-menu:whatsapp:full:v2",
                        )
                        second_url = generar_audio(
                            "Menu principal. Opcion 1, Reclamos. Opcion 2, Turnos.",
                            cache_namespace="whatsapp:menu:junin:main-menu:whatsapp:full:v2",
                        )
                        public_config = get_tts_audio_cache_public_config()

                self.assertEqual(first_url, second_url)
                self.assertTrue(first_url.startswith("https://cdn.chatboc.ar/audio/static/audio_cache/"))
                self.assertNotIn("Reclamos", first_url)
                self.assertNotIn("junin", first_url.rsplit("/", 1)[-1])
                self.assertEqual(public_config["public_url_mode"], "cdn")
                self.assertTrue(public_config["cdn_configured"])
                self.assertEqual(public_config["cdn_host"], "cdn.chatboc.ar")
                self.assertEqual(public_config["contract_version"], "tts.audio_cache_publication.v1")
                self.assertEqual(public_config["recommended_provider"], "cloudflare")
                self.assertTrue(public_config["edge_ready"])
                self.assertEqual(public_config["cache_control"], "public, max-age=31536000, immutable")
                self.assertEqual(public_config["next_action"], "monitor_hit_rate")
                self.assertFalse(public_config["privacy"]["content_text_exposed"])
                self.assertFalse(public_config["privacy"]["pii_in_url"])
                mock_openai_tts.assert_called_once()
                metrics = get_tts_cache_metrics()
                self.assertEqual(metrics["cache_misses"], 1)
                self.assertEqual(metrics["cache_hits"], 1)
            finally:
                os.chdir(original_cwd)

    def test_public_config_marks_cloudflare_publication_pending_without_cdn_env(self):
        with patch.dict(
            os.environ,
            {
                "TTS_AUDIO_CACHE_PUBLIC_BASE_URL": "",
                "CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL": "",
            },
            clear=False,
        ):
            public_config = get_tts_audio_cache_public_config()

        self.assertEqual(public_config["public_url_mode"], "backend_static")
        self.assertFalse(public_config["cdn_configured"])
        self.assertFalse(public_config["edge_ready"])
        self.assertEqual(public_config["next_action"], "configure_cloudflare_audio_cache_public_base_url")
        self.assertEqual(public_config["headers"]["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(
            {item["name"]: item["configured"] for item in public_config["required_env"]},
            {
                "CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL": False,
                "TTS_AUDIO_CACHE_PUBLIC_BASE_URL": False,
            },
        )

    def test_warmup_returns_cache_safe_summary_without_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                generated = Path("static/audio_responses/generated.mp3")
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(b"audio")

                entry = {
                    "tts_cache_text": "Menu principal. Opcion 1, Reclamos.",
                    "tts_cache_namespace": "whatsapp:menu:junin:quick-menu:widget:full:v1",
                }

                with patch.dict(os.environ, {"TTS_CACHE_ENABLED": "true"}, clear=False):
                    with patch(
                        "services.openai_tts_bridge.generar_audio_openai",
                        return_value=str(generated),
                    ) as mock_openai_tts:
                        first = warm_tts_cache([entry])
                        second = warm_tts_cache([entry])

                self.assertEqual(first["ready"], 1)
                self.assertEqual(second["ready"], 1)
                self.assertEqual(mock_openai_tts.call_count, 1)
                self.assertEqual(first["items"][0]["status"], "ready")
                self.assertNotIn("tts_cache_text", first["items"][0])
                self.assertNotIn("audio_text", first["items"][0])
                self.assertIn("/static/audio_cache/", first["items"][0]["audio_url"])
                metrics = get_tts_cache_metrics()
                self.assertEqual(metrics["warmup_requests"], 2)
                self.assertEqual(metrics["warmup_successes"], 2)
                self.assertEqual(metrics["cache_hits"], 1)
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
