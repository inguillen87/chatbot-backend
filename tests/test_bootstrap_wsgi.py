from __future__ import annotations

import json
import os
import threading
import time
import unittest
from io import BytesIO
from unittest.mock import patch

from bootstrap_wsgi import (
    LazyApplication,
    _safe_request_wait_seconds,
    _warmup_delay_seconds,
)


class LazyApplicationTests(unittest.TestCase):
    def test_vercel_warmup_delay_default_and_override_stay_below_readiness_budget(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_warmup_delay_seconds(), 0.1)
        with patch.dict(
            os.environ,
            {"VERCEL_WSGI_WARMUP_DELAY_SECONDS": "9"},
            clear=True,
        ):
            self.assertEqual(_warmup_delay_seconds(), 0.5)
        with patch.dict(
            os.environ,
            {"VERCEL_WSGI_WARMUP_DELAY_SECONDS": "0.25"},
            clear=True,
        ):
            self.assertEqual(_warmup_delay_seconds(), 0.25)

    def test_safe_request_wait_default_and_override_are_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_safe_request_wait_seconds(), 2.0)
        with patch.dict(
            os.environ,
            {"VERCEL_WSGI_SAFE_REQUEST_WAIT_SECONDS": "99"},
            clear=True,
        ):
            self.assertEqual(_safe_request_wait_seconds(), 4.0)
        with patch.dict(
            os.environ,
            {"VERCEL_WSGI_SAFE_REQUEST_WAIT_SECONDS": "-3"},
            clear=True,
        ):
            self.assertEqual(_safe_request_wait_seconds(), 0.0)
        with patch.dict(
            os.environ,
            {"VERCEL_WSGI_SAFE_REQUEST_WAIT_SECONDS": "invalid"},
            clear=True,
        ):
            self.assertEqual(_safe_request_wait_seconds(), 2.0)

    def test_loader_is_deferred_until_first_request_and_cached(self) -> None:
        loads: list[str] = []

        def target(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [environ["PATH_INFO"].encode()]

        def loader():
            loads.append("loaded")
            return target

        application = LazyApplication(loader)
        self.assertEqual(loads, [])

        statuses: list[str] = []
        result = application(
            {"PATH_INFO": "/health"},
            lambda status, headers: statuses.append(status),
        )

        self.assertEqual(result, [b"/health"])
        self.assertEqual(statuses, ["200 OK"])
        self.assertEqual(loads, ["loaded"])

        application(
            {"PATH_INFO": "/api/version"},
            lambda status, headers: statuses.append(status),
        )
        self.assertEqual(loads, ["loaded"])

    def test_concurrent_first_requests_only_load_once(self) -> None:
        load_count = 0
        count_lock = threading.Lock()

        def target(environ, start_response):
            start_response("200 OK", [])
            return [b"ok"]

        def loader():
            nonlocal load_count
            time.sleep(0.02)
            with count_lock:
                load_count += 1
            return target

        application = LazyApplication(loader)
        threads = [
            threading.Thread(
                target=application,
                args=({}, lambda status, headers: None),
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(load_count, 1)

    def test_rejects_non_callable_application(self) -> None:
        application = LazyApplication(lambda: object())

        with self.assertRaisesRegex(TypeError, "not callable"):
            application({}, lambda status, headers: None)

    def test_safe_http_methods_wait_then_dispatch_exactly_once(self) -> None:
        for method, path in (
            ("GET", "/api/v2/demo/catalog"),
            ("HEAD", "/health"),
            ("OPTIONS", "/api/v2/tickets"),
        ):
            with self.subTest(method=method, path=path):
                loader_started = threading.Event()
                release_loader = threading.Event()
                loads: list[str] = []
                dispatches: list[tuple[str, str]] = []

                def target(environ, start_response):
                    dispatches.append(
                        (environ["REQUEST_METHOD"], environ["PATH_INFO"])
                    )
                    start_response("200 OK", [("Content-Type", "text/plain")])
                    return [environ["PATH_INFO"].encode()]

                def loader():
                    loads.append("loaded")
                    loader_started.set()
                    release_loader.wait(1)
                    return target

                application = LazyApplication(
                    loader,
                    background_warmup=True,
                    warmup_delay_seconds=0.0,
                    safe_request_wait_seconds=0.5,
                )
                statuses: list[str] = []
                responses: list[list[bytes]] = []
                request_thread = threading.Thread(
                    target=lambda: responses.append(
                        application(
                            {"PATH_INFO": path, "REQUEST_METHOD": method},
                            lambda status, response_headers: statuses.append(status),
                        )
                    )
                )
                request_thread.start()
                self.assertTrue(loader_started.wait(0.5))
                self.assertTrue(request_thread.is_alive())
                self.assertEqual(dispatches, [])

                release_loader.set()
                request_thread.join(0.5)

                self.assertFalse(request_thread.is_alive())
                self.assertEqual(statuses, ["200 OK"])
                self.assertEqual(responses, [[path.encode()]])
                self.assertEqual(dispatches, [(method, path)])
                self.assertEqual(loads, ["loaded"])

    def test_concurrent_cold_requests_wait_on_one_background_load(self) -> None:
        loader_started = threading.Event()
        release_loader = threading.Event()
        load_count = 0
        count_lock = threading.Lock()
        dispatch_ids: list[int] = []

        def target(environ, start_response):
            with count_lock:
                dispatch_ids.append(environ["chatboc.request_id"])
            start_response("200 OK", [])
            return [str(environ["chatboc.request_id"]).encode()]

        def loader():
            nonlocal load_count
            with count_lock:
                load_count += 1
            loader_started.set()
            release_loader.wait(1)
            return target

        application = LazyApplication(
            loader,
            background_warmup=True,
            warmup_delay_seconds=0.01,
            safe_request_wait_seconds=0.5,
        )

        statuses: list[str] = []
        statuses_lock = threading.Lock()

        def capture_status(status: str, headers: list[tuple[str, str]]) -> None:
            del headers
            with statuses_lock:
                statuses.append(status)

        def request(request_id: int, path: str, method: str) -> None:
            application(
                {
                    "PATH_INFO": path,
                    "REQUEST_METHOD": method,
                    "chatboc.request_id": request_id,
                },
                capture_status,
            )

        requests = (
            ("/api/v2/demo/catalog", "GET"),
            ("/api/app/me/tenants", "GET"),
            ("/api/v2/tickets", "HEAD"),
            ("/api/admin/encuestas/632/publicar", "OPTIONS"),
        )
        threads = [
            threading.Thread(
                target=request,
                args=(index, *requests[index % len(requests)]),
            )
            for index in range(12)
        ]
        started_at = time.monotonic()
        for thread in threads:
            thread.start()
        self.assertTrue(loader_started.wait(0.5))
        release_loader.set()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started_at

        self.assertEqual(load_count, 1)
        self.assertEqual(statuses, ["200 OK"] * 12)
        self.assertEqual(sorted(dispatch_ids), list(range(12)))
        self.assertLess(elapsed, 0.5)

    def test_cold_mutations_fail_fast_and_only_an_explicit_retry_dispatches(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                loader_started = threading.Event()
                release_loader = threading.Event()
                dispatch_count = 0

                def target(environ, start_response):
                    nonlocal dispatch_count
                    dispatch_count += 1
                    start_response("200 OK", [])
                    return [environ["PATH_INFO"].encode()]

                def loader():
                    loader_started.set()
                    release_loader.wait(1)
                    return target

                application = LazyApplication(
                    loader,
                    background_warmup=True,
                    warmup_delay_seconds=0.0,
                    safe_request_wait_seconds=0.5,
                )

                shed_statuses: list[str] = []
                started_at = time.monotonic()
                shed_response = application(
                    {"PATH_INFO": "/api/v2/tickets", "REQUEST_METHOD": method},
                    lambda status, headers: shed_statuses.append(status),
                )
                shed_elapsed = time.monotonic() - started_at

                self.assertTrue(loader_started.wait(0.2))
                self.assertLess(shed_elapsed, 0.2)
                self.assertEqual(shed_statuses, ["503 Service Unavailable"])
                self.assertEqual(
                    json.loads(b"".join(shed_response))["reason_code"],
                    "application_initializing",
                )
                self.assertEqual(dispatch_count, 0)

                release_loader.set()
                self.assertTrue(application._ready.wait(0.5))
                self.assertEqual(dispatch_count, 0)

                retry_statuses: list[str] = []
                retry_response = application(
                    {"PATH_INFO": "/api/v2/tickets", "REQUEST_METHOD": method},
                    lambda status, headers: retry_statuses.append(status),
                )
                self.assertEqual(retry_statuses, ["200 OK"])
                self.assertEqual(retry_response, [b"/api/v2/tickets"])
                self.assertEqual(dispatch_count, 1)

    def test_read_wait_is_bounded_then_returns_retryable_contract(self) -> None:
        loader_started = threading.Event()
        release_loader = threading.Event()

        def target(environ, start_response):
            start_response("200 OK", [])
            return [b"ok"]

        def loader():
            loader_started.set()
            release_loader.wait(1)
            return target

        application = LazyApplication(
            loader,
            background_warmup=True,
            warmup_delay_seconds=0.0,
            safe_request_wait_seconds=0.03,
        )
        statuses: list[str] = []
        started_at = time.monotonic()
        response_headers: list[tuple[str, str]] = []
        response = application(
            {"PATH_INFO": "/api/v2/demo/catalog", "REQUEST_METHOD": "GET"},
            lambda status, headers: (
                statuses.append(status),
                response_headers.extend(headers),
            ),
        )
        elapsed = time.monotonic() - started_at

        self.assertTrue(loader_started.wait(0.2))
        self.assertGreaterEqual(elapsed, 0.02)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(statuses, ["503 Service Unavailable"])
        self.assertEqual(
            json.loads(b"".join(response))["reason_code"],
            "application_initializing",
        )
        self.assertEqual(dict(response_headers)["Retry-After"], "2")
        release_loader.set()
        self.assertTrue(application._ready.wait(0.5))

    def test_waiting_mutation_preserves_wsgi_input_and_headers(self) -> None:
        loader_started = threading.Event()
        release_loader = threading.Event()
        observed: list[tuple[bytes, str]] = []
        request_body = b'{"ticket_id":419,"message":"seguimiento"}'

        def target(environ, start_response):
            observed.append(
                (
                    environ["wsgi.input"].read(),
                    environ["HTTP_IDEMPOTENCY_KEY"],
                )
            )
            start_response("200 OK", [])
            return [b"accepted"]

        def loader():
            loader_started.set()
            release_loader.wait(1)
            return target

        application = LazyApplication(
            loader,
            background_warmup=True,
            warmup_delay_seconds=0.0,
            safe_request_wait_seconds=0.5,
        )
        statuses: list[str] = []
        environ = {
            "PATH_INFO": "/api/v2/tickets/419/reply",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(request_body)),
            "CONTENT_TYPE": "application/json",
            "HTTP_IDEMPOTENCY_KEY": "reply-419-1",
            "wsgi.input": BytesIO(request_body),
        }
        first_response = application(
            environ,
            lambda status, headers: statuses.append(status),
        )
        self.assertTrue(loader_started.wait(0.5))
        self.assertEqual(observed, [])
        self.assertEqual(statuses, ["503 Service Unavailable"])
        self.assertEqual(
            json.loads(b"".join(first_response))["reason_code"],
            "application_initializing",
        )

        release_loader.set()
        self.assertTrue(application._ready.wait(0.5))

        retry_response = application(
            environ,
            lambda status, headers: statuses.append(status),
        )

        self.assertEqual(statuses, ["503 Service Unavailable", "200 OK"])
        self.assertEqual(retry_response, [b"accepted"])
        self.assertEqual(observed, [(request_body, "reply-419-1")])

    def test_background_warmup_fails_closed_without_exposing_exception(self) -> None:
        secret_detail = "private-runtime-secret"

        def loader():
            raise RuntimeError(secret_detail)

        application = LazyApplication(
            loader,
            background_warmup=True,
            warmup_delay_seconds=0.01,
        )
        application(
            {"PATH_INFO": "/api/v2/tickets", "REQUEST_METHOD": "POST"},
            lambda status, headers: None,
        )
        self.assertTrue(application._ready.wait(0.5))
        statuses: list[str] = []
        response = application(
            {"PATH_INFO": "/api/v2/tickets", "REQUEST_METHOD": "POST"},
            lambda status, headers: statuses.append(status),
        )
        body = b"".join(response).decode("utf-8")

        self.assertEqual(statuses, ["503 Service Unavailable"])
        self.assertEqual(
            json.loads(body)["reason_code"],
            "application_initialization_failed",
        )
        self.assertNotIn(secret_detail, body)


if __name__ == "__main__":
    unittest.main()
