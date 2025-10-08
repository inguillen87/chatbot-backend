import base64
import os
import unittest
from unittest.mock import mock_open, patch

import httpx

from services.cohere_tts_bridge import generar_audio_cohere


class _DummyResponse:
    def __init__(self, *, headers, json_payload=None, content=b""):
        self.headers = headers
        self._json_payload = json_payload
        self.content = content

    def json(self):
        return self._json_payload

    def raise_for_status(self):
        return None


class _DummyClient:
    def __init__(self, response):
        self._response = response
        self.last_request = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, endpoint, *, json=None, headers=None):
        self.last_request = {
            "endpoint": endpoint,
            "json": json,
            "headers": headers,
        }
        return self._response


class TestCohereTTSBridge(unittest.TestCase):
    @patch("services.cohere_tts_bridge.uuid.uuid4", return_value="1234")
    @patch("services.cohere_tts_bridge.os.makedirs")
    @patch("services.cohere_tts_bridge.open", new_callable=mock_open)
    @patch("services.cohere_tts_bridge.httpx.Client")
    def test_decodes_json_payload(
        self,
        mock_httpx_client,
        mock_file,
        mock_makedirs,
        mock_uuid,
    ):
        audio_bytes = b"cohere-audio"
        encoded = base64.b64encode(audio_bytes).decode()
        response = _DummyResponse(
            headers={"Content-Type": "application/json"},
            json_payload={
                "generations": [
                    {
                        "audio": {"mp3_base64": encoded},
                    }
                ]
            },
        )
        dummy_client = _DummyClient(response)
        mock_httpx_client.return_value = dummy_client

        with patch.dict(os.environ, {"COHERE_API_KEY": "test"}):
            result = generar_audio_cohere("hola, mundo", voice="latam")

        self.assertEqual(
            result,
            "/static/audio_responses/cohere-1234.mp3",
        )

        posted = dummy_client.last_request
        self.assertIsNotNone(posted)
        self.assertEqual(
            posted["endpoint"], "https://api.cohere.ai/v1/audio/generate"
        )
        self.assertEqual(posted["json"]["voice"], "latam")
        handle = mock_file()
        handle.write.assert_called_once_with(audio_bytes)

    @patch("services.cohere_tts_bridge.httpx.Client")
    def test_handles_http_error(self, mock_httpx_client):
        request = httpx.Request("POST", "https://api.cohere.ai/v1/audio/generate")
        response = httpx.Response(status_code=404, request=request)

        class ErrorResponse(_DummyResponse):
            def raise_for_status(self):
                raise httpx.HTTPStatusError("not found", request=request, response=response)

        dummy_client = _DummyClient(ErrorResponse(headers={}))
        mock_httpx_client.return_value = dummy_client

        with patch.dict(os.environ, {"COHERE_API_KEY": "test"}):
            result = generar_audio_cohere("hola")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
