"""Speech failure diagnostics must disclose only bounded, allowlisted metadata."""

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services import openai_tts_bridge as bridge


CANARY = 'SYNTHETIC_CANARY_NOT_A_REAL_SECRET_5f90c2'


@pytest.fixture
def sdk_failure(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-not-a-secret')
    monkeypatch.setenv('OPENAI_TTS_MODEL', 'synthetic-model')
    monkeypatch.setenv('OPENAI_TTS_TIMEOUT_SECONDS', '0.25')
    sdk_client = MagicMock()
    bridge.clear_tts_cache()
    with patch.object(bridge.httpx, 'Client', return_value=MagicMock()), patch.object(bridge.openai, 'OpenAI', return_value=sdk_client):
        yield sdk_client.audio.speech.create
    bridge.clear_tts_cache()


def sdk_rate_error(code):
    response = httpx.Response(429, request=httpx.Request('POST', 'https://synthetic.invalid/speech'),
                              headers={'x-request-id': CANARY, 'authorization': CANARY})
    return bridge.openai.RateLimitError(CANARY, response=response,
        body={'code': code, 'message': CANARY, 'param': CANARY,
              'nested_private_body': {'token': CANARY}})


@pytest.mark.parametrize('code', ['insufficient_quota', 'rate_limit_exceeded'])
def test_rate_limit_error_logs_status_and_only_allowlisted_code(sdk_failure, caplog, code):
    sdk_failure.side_effect = sdk_rate_error(code)
    with caplog.at_level(logging.ERROR, logger='services.openai_tts_bridge'):
        assert bridge.generar_audio_openai('Texto ficticio diagnóstico') is None
    assert 'http_status=429' in caplog.text
    assert 'error_code=' + code in caplog.text
    assert 'RateLimitError' in caplog.text
    assert CANARY not in caplog.text
    assert 'nested_private_body' not in caplog.text
    assert 'synthetic.invalid' not in caplog.text


@pytest.mark.parametrize('code', [None, CANARY, {'arbitrary_body': CANARY}, 429, 'INSUFFICIENT_QUOTA'])
def test_unknown_or_nonstring_sdk_code_stays_unclassified(sdk_failure, caplog, code):
    sdk_failure.side_effect = sdk_rate_error(code)
    with caplog.at_level(logging.ERROR, logger='services.openai_tts_bridge'):
        assert bridge.generar_audio_openai('Texto ficticio sin enum seguro') is None
    assert 'http_status=429' in caplog.text
    assert 'error_code=unclassified' in caplog.text
    assert CANARY not in caplog.text
    assert 'arbitrary_body' not in caplog.text


@pytest.mark.parametrize('status', [None, True, '429', 99, 600, CANARY])
def test_invalid_status_is_not_serialized(sdk_failure, caplog, status):
    class SyntheticError(Exception):
        status_code = status
        code = CANARY

    sdk_failure.side_effect = SyntheticError(CANARY)
    with caplog.at_level(logging.ERROR, logger='services.openai_tts_bridge'):
        assert bridge.generar_audio_openai('Texto ficticio status no confiable') is None
    assert 'http_status=None' in caplog.text
    assert 'error_code=unclassified' in caplog.text
    assert CANARY not in caplog.text


def test_exception_diagnostic_properties_cannot_replace_safe_failure(sdk_failure, caplog):
    class SyntheticError(Exception):
        @property
        def status_code(self):
            raise RuntimeError(CANARY)

        @property
        def code(self):
            raise RuntimeError(CANARY)

        @property
        def body(self):
            raise AssertionError('Diagnostic must never inspect arbitrary body')

        @property
        def headers(self):
            raise AssertionError('Diagnostic must never inspect headers')

    sdk_failure.side_effect = SyntheticError(CANARY)
    with caplog.at_level(logging.ERROR, logger='services.openai_tts_bridge'):
        assert bridge.generar_audio_openai('Texto ficticio propiedades') is None
    assert 'http_status=None' in caplog.text
    assert 'error_code=unclassified' in caplog.text
    assert CANARY not in caplog.text
