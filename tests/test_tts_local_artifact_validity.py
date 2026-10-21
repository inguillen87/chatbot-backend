"""Local synthetic files and HTTP views; no codec or provider acceptance."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from flask import Flask

from services import openai_tts_bridge as bridge
from services import tts_orchestrator as tts
from services.tts_audio_artifacts import is_nonempty_audio_file


class TestTTSLocalArtifactValidity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.restore_directory)
        self.addCleanup(bridge.clear_tts_cache)
        bridge.clear_tts_cache()
        tts.reset_tts_cache_metrics()
        self.env = patch.dict(os.environ, {
            'TTS_CACHE_ENABLED': 'true', 'TTS_PROVIDER_ORDER': 'openai',
            'TTS_AUDIO_CACHE_PUBLIC_BASE_URL': '',
            'CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL': '',
            'OPENAI_TTS_VOICE': 'nova', 'OPENAI_TTS_MODEL': 'synthetic-model',
            'OPENAI_TTS_STYLE': '', 'TTS_SPEECH_SPEED': '0.95',
            'TTS_COHERE_ENABLED': 'false', 'COHERE_ENABLED': 'false',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network_patches = (
            patch.object(socket, 'create_connection', side_effect=AssertionError('No network in fixture')),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('No network in fixture')),
        )
        for blocker in self.network_patches:
            blocker.start()
            self.addCleanup(blocker.stop)
        self.generated = Path('static/audio_responses/synthetic.mp3')
        self.generated.parent.mkdir(parents=True)
        self.app = Flask('tts-local-artifact', root_path=self.temp.name,
                         static_folder=str(Path(self.temp.name) / 'static'),
                         static_url_path='/static')

    def restore_directory(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def generate(self):
        return tts.generar_audio('Menu de prueba ficticia', cache_namespace='synthetic:menu:v1')

    def provider_url(self):
        return '/' + self.generated.as_posix()

    def assert_downloadable_synthetic_file(self, url, expected=b'synthetic-audio'):
        self.assertIsInstance(url, str)
        path = urlparse(url).path
        get = self.app.test_client().get(path)
        head = self.app.test_client().head(path)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(get.data, expected)
        self.assertEqual(int(head.headers['Content-Length']), len(expected))

    def test_file_check_rejects_missing_empty_directory_and_oserror(self):
        self.assertFalse(is_nonempty_audio_file(str(self.generated)))
        self.generated.write_bytes(b'')
        self.assertFalse(is_nonempty_audio_file(str(self.generated)))
        self.assertFalse(is_nonempty_audio_file(str(self.generated.parent)))
        self.generated.write_bytes(b'synthetic-audio')
        self.assertTrue(is_nonempty_audio_file(str(self.generated)))
        with patch('services.tts_audio_artifacts.os.stat', side_effect=PermissionError):
            self.assertFalse(is_nonempty_audio_file(str(self.generated)))

    def test_empty_cache_regenerates_and_replaces_file(self):
        self.generated.write_bytes(b'synthetic-audio')
        with patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()) as provider:
            first = self.generate()
            Path(urlparse(first).path.lstrip('/')).write_bytes(b'')
            second = self.generate()
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(first, second)
        self.assert_downloadable_synthetic_file(second)

    def test_cache_directory_regenerates_without_removing_directory(self):
        self.generated.write_bytes(b'synthetic-audio')
        with patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()) as provider:
            first = self.generate()
            cached = Path(urlparse(first).path.lstrip('/'))
            cached.unlink()
            cached.mkdir()
            second = self.generate()
        self.assertEqual(provider.call_count, 2)
        self.assertTrue(cached.is_dir())
        self.assertIn('/static/audio_responses/', second)
        self.assert_downloadable_synthetic_file(second)

    def test_cache_created_while_waiting_lock_is_checked_and_reused(self):
        self.generated.write_bytes(b'synthetic-audio')
        with patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()) as provider:
            first = self.generate()
            cached = Path(urlparse(first).path.lstrip('/'))
            cached.write_bytes(b'')
            provider.reset_mock()

            @contextmanager
            def lock_with_prior_request_completion(_lock):
                cached.write_bytes(b'synthetic-audio')
                yield

            with patch.object(tts, '_bounded_generation_lock', lock_with_prior_request_completion):
                second = self.generate()
        provider.assert_not_called()
        self.assertEqual(first, second)
        self.assert_downloadable_synthetic_file(second)

    def test_invalid_provider_file_returns_none_with_cache_on_or_off(self):
        for cache_setting in ('true', 'false'):
            for file_state in ('missing', 'empty', 'directory'):
                with self.subTest(cache=cache_setting, state=file_state):
                    if self.generated.exists():
                        if self.generated.is_dir():
                            self.generated.rmdir()
                        else:
                            self.generated.unlink()
                    if file_state == 'empty':
                        self.generated.write_bytes(b'')
                    elif file_state == 'directory':
                        self.generated.mkdir()
                    tts.reset_tts_cache_metrics()
                    with patch.dict(os.environ, {'TTS_CACHE_ENABLED': cache_setting}), patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()):
                        self.assertIsNone(self.generate())
                    metrics = tts.get_tts_cache_metrics()
                    self.assertEqual(metrics['provider_successes'], 0)
                    self.assertEqual(metrics['provider_failures'], 1)
                    self.assertEqual(metrics['generation_failures'], 1)

    def test_invalid_primary_artifact_continues_configured_fallback(self):
        alternate = Path('static/audio_responses/alternate.mp3')
        alternate.write_bytes(b'synthetic-audio')
        with patch.dict(os.environ, {'TTS_PROVIDER_ORDER': 'openai,cohere', 'TTS_COHERE_ENABLED': 'true'}), patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()), patch('services.cohere_tts_bridge.generar_audio_cohere', return_value='/' + alternate.as_posix()) as fallback:
            result = self.generate()
        fallback.assert_called_once()
        self.assert_downloadable_synthetic_file(result)
        self.assertEqual(tts.get_tts_cache_metrics()['provider_failures'], 1)
        self.assertEqual(tts.get_tts_cache_metrics()['provider_successes'], 1)

    def test_empty_cache_copy_keeps_valid_source_and_cleans_temporary(self):
        self.generated.write_bytes(b'synthetic-audio')

        def empty_copy(_source, destination):
            Path(destination).write_bytes(b'')

        with patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()), patch.object(tts.shutil, 'copyfile', side_effect=empty_copy):
            result = self.generate()
        self.assertEqual(list(Path('static/audio_cache').iterdir()), [])
        self.assertIn('/static/audio_responses/', result)
        self.assert_downloadable_synthetic_file(result)

    def test_disappeared_source_during_failed_copy_is_not_published(self):
        self.generated.write_bytes(b'synthetic-audio')

        def removed_source(_source, _destination):
            self.generated.unlink()
            raise FileNotFoundError('synthetic source removed')

        with patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()), patch.object(tts.shutil, 'copyfile', side_effect=removed_source):
            self.assertIsNone(self.generate())
        self.assertEqual(list(Path('static/audio_cache').iterdir()), [])

    def test_nonempty_source_cache_disabled_and_absolute_url_keep_contract(self):
        self.generated.write_bytes(b'synthetic-audio')
        with patch.dict(os.environ, {'TTS_CACHE_ENABLED': 'false'}), patch.object(bridge, 'generar_audio_openai', return_value=self.provider_url()):
            self.assert_downloadable_synthetic_file(self.generate())
        with patch.object(bridge, 'generar_audio_openai', return_value='https://synthetic.invalid/public-audio.mp3'):
            self.assertEqual(self.generate(), 'https://synthetic.invalid/public-audio.mp3')

    def test_bridge_stale_cache_regenerates_and_valid_cache_is_reused(self):
        fake_client = MagicMock()
        response = fake_client.audio.speech.create.return_value

        def write_output(path):
            Path(path).write_bytes(b'synthetic-audio')

        response.stream_to_file.side_effect = write_output
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-not-a-secret'}), patch.object(bridge.openai, 'OpenAI', return_value=fake_client), patch.object(bridge.httpx, 'Client', return_value=MagicMock()):
            first = bridge.generar_audio_openai('Prueba ficticia', voice='nova', model='synthetic-model')
            Path(first.lstrip('/')).unlink()
            second = bridge.generar_audio_openai('Prueba ficticia', voice='nova', model='synthetic-model')
            third = bridge.generar_audio_openai('Prueba ficticia', voice='nova', model='synthetic-model')
        self.assertNotEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(fake_client.audio.speech.create.call_count, 2)
        self.assert_downloadable_synthetic_file(second)

    def test_bridge_empty_or_missing_stream_output_is_not_cached(self):
        for file_state in ('empty', 'missing'):
            with self.subTest(state=file_state):
                fake_client = MagicMock()

                def write_output(path):
                    if file_state == 'empty':
                        Path(path).write_bytes(b'')

                fake_client.audio.speech.create.return_value.stream_to_file.side_effect = write_output
                bridge.clear_tts_cache()
                with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-not-a-secret'}), patch.object(bridge.openai, 'OpenAI', return_value=fake_client), patch.object(bridge.httpx, 'Client', return_value=MagicMock()):
                    self.assertIsNone(bridge.generar_audio_openai('Prueba ficticia', voice='nova', model='synthetic-model'))
                self.assertEqual(len(bridge._TTS_CACHE), 0)


if __name__ == '__main__':
    unittest.main()
