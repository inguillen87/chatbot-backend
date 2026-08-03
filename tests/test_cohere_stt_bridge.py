from unittest.mock import MagicMock, patch

import httpx

from services.cohere_stt_bridge import transcribir_audio_cohere


def test_cohere_stt_test_policy_blocks_before_client(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("COHERE_API_KEY", "private-test-key")
    monkeypatch.delenv("COHERE_ALLOW_NETWORK_IN_TESTS", raising=False)

    with patch("services.cohere_stt_bridge.httpx.Client") as client:
        assert transcribir_audio_cohere(b"audio", "audio/ogg") is None

    client.assert_not_called()


def test_cohere_stt_opt_in_returns_transcript_without_logging_payload(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("COHERE_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.setenv("COHERE_API_KEY", "private-test-key")
    response = MagicMock()
    response.json.return_value = {"text": "  hola municipio  "}
    client = MagicMock()
    client.post.return_value = response
    context_manager = MagicMock()
    context_manager.__enter__.return_value = client

    with patch("services.cohere_stt_bridge.httpx.Client", return_value=context_manager):
        assert transcribir_audio_cohere(b"audio", "audio/ogg") == "hola municipio"


def test_cohere_stt_provider_error_log_redacts_exception(monkeypatch, caplog):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("COHERE_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.setenv("COHERE_API_KEY", "private-test-key")
    sensitive = "DNI=32877851 token=secret"
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError(sensitive)
    context_manager = MagicMock()
    context_manager.__enter__.return_value = client

    with patch("services.cohere_stt_bridge.httpx.Client", return_value=context_manager):
        assert transcribir_audio_cohere(b"audio", "audio/ogg") is None

    assert "ConnectError" in caplog.text
    assert sensitive not in caplog.text
    assert "32877851" not in caplog.text


def test_cohere_stt_missing_text_log_redacts_response(monkeypatch, caplog):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("COHERE_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.setenv("COHERE_API_KEY", "private-test-key")
    sensitive = "DNI=32877851 token=secret"
    response = MagicMock()
    response.json.return_value = {"error": sensitive}
    client = MagicMock()
    client.post.return_value = response
    context_manager = MagicMock()
    context_manager.__enter__.return_value = client

    with patch("services.cohere_stt_bridge.httpx.Client", return_value=context_manager):
        assert transcribir_audio_cohere(b"audio", "audio/ogg") is None

    assert "response_type=dict" in caplog.text
    assert sensitive not in caplog.text
    assert "32877851" not in caplog.text
