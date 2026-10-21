"""Configured speech I/O bounds; synthetic bytes and clients, no provider I/O."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services import openai_tts_bridge as bridge
from services.outbox_execution_budget import activate_outbox_execution_budget


@pytest.fixture
def speech_clients(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-not-a-secret')
    monkeypatch.setenv('OPENAI_TTS_MODEL', 'synthetic-model')
    monkeypatch.setenv('OPENAI_TTS_DEFAULT_VOICE', 'nova')
    bridge.clear_tts_cache()
    http_client = MagicMock()
    sdk = MagicMock()
    response = sdk.return_value.audio.speech.create.return_value
    response.stream_to_file.side_effect = lambda path: Path(path).write_bytes(b'synthetic-audio-fixture')
    with patch.object(bridge.httpx, 'Client', http_client), patch.object(bridge.openai, 'OpenAI', sdk):
        yield http_client, sdk
    bridge.clear_tts_cache()


def assert_timeout_binding(clients, expected, *, cron=False):
    http_client, sdk = clients
    assert http_client.call_args.kwargs == {
        'proxy': None, 'trust_env': False, 'timeout': expected,
    }
    expected_sdk = {
        'api_key': 'synthetic-not-a-secret',
        'timeout': expected,
        'http_client': http_client.return_value,
    }
    if cron:
        expected_sdk['max_retries'] = 0
    assert sdk.call_args.kwargs == expected_sdk


def test_configured_timeout_applies_to_normal_speech_without_changing_retries(speech_clients, monkeypatch):
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', '0.25')
    assert bridge.generar_audio_openai('Texto ficticio normal') is not None
    assert_timeout_binding(speech_clients, 0.25)


@pytest.mark.parametrize('configured', ['', 'invalid', '0', '-2', 'nan', 'inf', '-inf'])
def test_invalid_timeout_uses_finite_positive_default(speech_clients, monkeypatch, configured):
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', configured)
    assert bridge.generar_audio_openai('Texto ficticio default') is not None
    assert_timeout_binding(speech_clients, 60.0)


def test_missing_timeout_uses_existing_sixty_second_default(speech_clients, monkeypatch):
    monkeypatch.delenv('OPENAI_TTS_TIMEOUT_SECONDS', raising=False)
    assert bridge.generar_audio_openai('Texto ficticio default ausente') is not None
    assert_timeout_binding(speech_clients, 60.0)


@pytest.mark.parametrize(('configured', 'expected'), [('60', 4.0), ('0.25', 0.25)])
def test_cron_keeps_cap_and_uses_shorter_configured_timeout(speech_clients, monkeypatch, configured, expected):
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', configured)
    with activate_outbox_execution_budget(deadline_monotonic=145.0, clock=lambda: 100.0):
        assert bridge.generar_audio_openai('Texto ficticio cron') is not None
    assert_timeout_binding(speech_clients, expected, cron=True)


def test_cron_keeps_remaining_deadline_reserve(speech_clients, monkeypatch):
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', '60')
    with activate_outbox_execution_budget(deadline_monotonic=145.0, clock=lambda: 137.0):
        assert bridge.generar_audio_openai('Texto ficticio reserva') is not None
    assert_timeout_binding(speech_clients, 2.0, cron=True)


def test_expired_cron_does_not_construct_provider_clients(speech_clients, monkeypatch):
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', '60')
    with activate_outbox_execution_budget(deadline_monotonic=145.0, clock=lambda: 139.0):
        # Existing bridge behavior returns None; do not claim global propagation.
        assert bridge.generar_audio_openai('Texto ficticio sin presupuesto') is None
    for constructor in speech_clients:
        constructor.assert_not_called()


def test_provider_timeout_keeps_no_audio_and_does_not_cache_a_url(speech_clients, monkeypatch):
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', '0.25')
    speech_clients[1].return_value.audio.speech.create.side_effect = TimeoutError('synthetic timeout')
    assert bridge.generar_audio_openai('Texto ficticio timeout') is None
    assert_timeout_binding(speech_clients, 0.25)
    assert not bridge._TTS_CACHE
