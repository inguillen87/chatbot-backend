import base64
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import multimodal_analyzer
from services import vision_fallback_service


class _FakeResponse:
    def __init__(self, *, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        return iter(self._chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http failure")

    def close(self):
        self.closed = True


def _public_dns(*_args, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


class TestMultimodalAnalyzer(unittest.TestCase):
    def test_municipal_result_is_direct_and_request_is_strict_responses(self):
        client = MagicMock()
        client.responses.create.return_value = SimpleNamespace(
            output_text=(
                '{"intent":"crear_reclamo","data":{"categoria":"Arreglo de calle",'
                '"descripcion":"Bache profundo"},"items":[],"productos":[]}'
            ),
            output=[],
        )
        image_bytes = b"\x89PNG\r\n\x1a\nimage"

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "claim.png"
            image_path.write_bytes(image_bytes)
            with (
                patch.object(
                    vision_fallback_service,
                    "_get_openai_client",
                    return_value=client,
                ),
                patch.dict(
                    os.environ,
                    {"OPENAI_SAFETY_IDENTIFIER_SECRET": "test-secret"},
                    clear=False,
                ),
            ):
                result = multimodal_analyzer.analizar_imagen_openai(
                    str(image_path),
                    "Clasifica el reclamo municipal",
                )

        self.assertEqual(
            result,
            {
                "intent": "crear_reclamo",
                "data": {
                    "categoria": "Arreglo de calle",
                    "descripcion": "Bache profundo",
                },
            },
        )
        request = client.responses.create.call_args.kwargs
        self.assertFalse(request["store"])
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertIs(
            request["text"]["format"]["schema"],
            multimodal_analyzer.MULTIMODAL_OUTPUT_SCHEMA,
        )
        self.assertEqual(request["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(len(request["safety_identifier"]), 64)
        self.assertNotEqual(request["safety_identifier"], str(image_path))

    def test_remote_download_is_streamed_bounded_and_proxy_independent(self):
        image_bytes = b"\xff\xd8\xffimage"
        response = _FakeResponse(
            headers={
                "Content-Type": "image/jpeg; charset=binary",
                "Content-Length": str(len(image_bytes)),
            },
            chunks=(image_bytes[:4], image_bytes[4:]),
        )
        session = MagicMock()
        session.get.return_value = response

        with (
            patch("services.multimodal_analyzer.socket.getaddrinfo", _public_dns),
            patch("services.multimodal_analyzer.requests.Session", return_value=session),
        ):
            encoded = multimodal_analyzer.encode_image_to_base64(
                "https://images.example.test/photo.jpg"
            )

        self.assertEqual(encoded, base64.b64encode(image_bytes).decode("ascii"))
        self.assertFalse(session.trust_env)
        session.get.assert_called_once_with(
            "https://images.example.test/photo.jpg",
            allow_redirects=False,
            headers={
                "Accept": "image/gif, image/jpeg, image/png, image/webp"
            },
            stream=True,
            timeout=multimodal_analyzer.REMOTE_IMAGE_TIMEOUT,
        )
        self.assertTrue(response.closed)
        session.close.assert_called_once_with()

    def test_ssrf_private_address_is_rejected_before_http_send(self):
        session = MagicMock()
        private_dns = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with (
            patch(
                "services.multimodal_analyzer.socket.getaddrinfo",
                return_value=private_dns,
            ),
            patch("services.multimodal_analyzer.requests.Session", return_value=session),
        ):
            encoded = multimodal_analyzer.encode_image_to_base64(
                "https://localhost.example.test/private.jpg"
            )

        self.assertIsNone(encoded)
        session.get.assert_not_called()
        session.close.assert_called_once_with()

    def test_remote_size_and_type_are_enforced(self):
        too_large = _FakeResponse(
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(multimodal_analyzer.MAX_IMAGE_BYTES + 1),
            }
        )
        wrong_type = _FakeResponse(
            headers={"Content-Type": "text/html"},
            chunks=(b"\xff\xd8\xffimage",),
        )
        session = MagicMock()
        session.get.side_effect = [too_large, wrong_type]

        with (
            patch("services.multimodal_analyzer.socket.getaddrinfo", _public_dns),
            patch("services.multimodal_analyzer.requests.Session", return_value=session),
        ):
            first = multimodal_analyzer.encode_image_to_base64(
                "https://images.example.test/large.jpg"
            )
            second = multimodal_analyzer.encode_image_to_base64(
                "https://images.example.test/not-image"
            )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertTrue(too_large.closed)
        self.assertTrue(wrong_type.closed)

    def test_private_redirect_target_is_rejected_without_following_it(self):
        redirect = _FakeResponse(
            status_code=302,
            headers={"Location": "http://127.0.0.1/metadata"},
        )
        session = MagicMock()
        session.get.return_value = redirect

        def redirect_dns(hostname, *_args, **_kwargs):
            if hostname == "127.0.0.1":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", 80),
                    )
                ]
            return _public_dns()

        with (
            patch(
                "services.multimodal_analyzer.socket.getaddrinfo",
                redirect_dns,
            ),
            patch("services.multimodal_analyzer.requests.Session", return_value=session),
        ):
            encoded = multimodal_analyzer.encode_image_to_base64(
                "https://images.example.test/redirect"
            )

        self.assertIsNone(encoded)
        session.get.assert_called_once()

    def test_ambiguous_openai_failure_is_not_retried(self):
        with (
            patch.object(
                multimodal_analyzer,
                "analizar_imagen_openai",
                side_effect=vision_fallback_service.OpenAIAmbiguousVisionFailure(),
            ) as analyze,
            self.assertLogs("services.multimodal_analyzer", level="WARNING") as logs,
        ):
            result = multimodal_analyzer.analizar_imagen_con_fallback(
                "https://images.example.test/photo.jpg",
                "prompt",
            )

        self.assertIsNone(result)
        analyze.assert_called_once()
        self.assertIn(
            "fallback suppressed reason=ambiguous_openai_outcome",
            "\n".join(logs.output),
        )


if __name__ == "__main__":
    unittest.main()
