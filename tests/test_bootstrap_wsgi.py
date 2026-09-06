from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from bootstrap_wsgi import (
    LazyApplication,
    _request_can_wait_for_application,
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
            self.assertEqual(_safe_request_wait_seconds(), 6.0)
        with patch.dict(
            os.environ,
            {"VERCEL_WSGI_SAFE_REQUEST_WAIT_SECONDS": "99"},
            clear=True,
        ):
            self.assertEqual(_safe_request_wait_seconds(), 10.0)
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
            self.assertEqual(_safe_request_wait_seconds(), 6.0)

    def test_only_reads_and_exact_demo_posts_can_join_bootstrap_wait(self) -> None:
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertTrue(
                    _request_can_wait_for_application(
                        {"REQUEST_METHOD": method, "PATH_INFO": "/api/anything"}
                    )
                )

        for path in (
            "/api/v2/demo/session",
            "/api/v2/demo/whatsapp-sandbox",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    _request_can_wait_for_application(
                        {"REQUEST_METHOD": "POST", "PATH_INFO": path}
                    )
                )

        for method, path in (
            ("POST", "/api/v2/tickets"),
            ("POST", "/api/v2/demo/session/"),
            ("PUT", "/api/v2/demo/session"),
            ("DELETE", "/api/v2/demo/whatsapp-sandbox"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(
                    _request_can_wait_for_application(
                        {"REQUEST_METHOD": method, "PATH_INFO": path}
                    )
                )

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

    def test_real_mutation_starts_background_warmup_fail_fast_and_is_cached(self) -> None:
        loader_started = threading.Event()
        release_loader = threading.Event()
        loads: list[str] = []

        def target(environ, start_response):
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
            warmup_delay_seconds=0.01,
        )
        self.assertFalse(loader_started.is_set())

        statuses: list[str] = []
        headers: list[tuple[str, str]] = []
        response = application(
            {"PATH_INFO": "/api/v2/tickets", "REQUEST_METHOD": "POST"},
            lambda status, response_headers: (
                statuses.append(status),
                headers.extend(response_headers),
            ),
        )

        self.assertEqual(statuses, ["503 Service Unavailable"])
        self.assertEqual(json.loads(b"".join(response)), {
            "contract_version": "chatboc.bootstrap.v1",
            "ok": False,
            "status_code": 503,
            "reason_code": "application_initializing",
            "retryable": True,
            "action_hint": "retry_after",
            "error": {
                "code": 503,
                "message": "La aplicacion se esta iniciando. Intentalo nuevamente.",
            },
        })
        self.assertIn(("Retry-After", "1"), headers)
        self.assertIn(("Cache-Control", "no-store"), headers)
        self.assertTrue(loader_started.wait(0.5))

        release_loader.set()
        self.assertTrue(application._ready.wait(0.5))
        statuses.clear()
        response = application(
            {"PATH_INFO": "/health", "REQUEST_METHOD": "GET"},
            lambda status, response_headers: statuses.append(status),
        )
        self.assertEqual(response, [b"/health"])
        self.assertEqual(statuses, ["200 OK"])
        self.assertEqual(loads, ["loaded"])

    def test_concurrent_safe_reads_wait_on_one_background_load(self) -> None:
        loader_started = threading.Event()
        release_loader = threading.Event()
        load_count = 0
        count_lock = threading.Lock()

        def target(environ, start_response):
            start_response("200 OK", [])
            return [b"ok"]

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

        def request(path: str) -> None:
            application(
                {"PATH_INFO": path, "REQUEST_METHOD": "GET"},
                capture_status,
            )

        paths = (
            "/api/v2/demo/catalog",
            "/api/app/me/tenants",
            "/api/pwa/anon-id",
        )
        threads = [
            threading.Thread(target=request, args=(paths[index % len(paths)],))
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
        self.assertLess(elapsed, 0.5)

    def test_real_mutations_never_wait_for_background_warmup(self) -> None:
        loader_started = threading.Event()
        release_loader = threading.Event()

        def target(environ, start_response):
            start_response("200 OK", [])
            return [environ["PATH_INFO"].encode()]

        def loader():
            loader_started.set()
            release_loader.wait(1)
            return target

        application = LazyApplication(
            loader,
            background_warmup=True,
            warmup_delay_seconds=0.01,
            safe_request_wait_seconds=0.5,
        )

        shed_statuses: list[str] = []
        started_at = time.monotonic()
        shed_response = application(
            {"PATH_INFO": "/api/v2/tickets", "REQUEST_METHOD": "POST"},
            lambda status, headers: shed_statuses.append(status),
        )
        shed_elapsed = time.monotonic() - started_at

        self.assertLess(shed_elapsed, 0.1)
        self.assertEqual(shed_statuses, ["503 Service Unavailable"])
        self.assertEqual(
            json.loads(b"".join(shed_response))["reason_code"],
            "application_initializing",
        )
        self.assertTrue(loader_started.wait(0.5))

        release_loader.set()
        self.assertTrue(application._ready.wait(0.5))

    def test_safe_read_wait_is_bounded_then_returns_retryable_contract(self) -> None:
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
        response = application(
            {"PATH_INFO": "/api/v2/demo/catalog", "REQUEST_METHOD": "GET"},
            lambda status, headers: statuses.append(status),
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
        release_loader.set()
        self.assertTrue(application._ready.wait(0.5))

    def test_exact_demo_posts_wait_and_dispatch_after_cold_start(self) -> None:
        for path in (
            "/api/v2/demo/session",
            "/api/v2/demo/whatsapp-sandbox",
        ):
            with self.subTest(path=path):
                load_count = 0

                def target(environ, start_response):
                    start_response("200 OK", [])
                    return [environ["PATH_INFO"].encode()]

                def loader():
                    nonlocal load_count
                    load_count += 1
                    time.sleep(0.02)
                    return target

                application = LazyApplication(
                    loader,
                    background_warmup=True,
                    warmup_delay_seconds=0.0,
                    safe_request_wait_seconds=0.2,
                )
                statuses: list[str] = []
                response = application(
                    {"PATH_INFO": path, "REQUEST_METHOD": "POST"},
                    lambda status, headers: statuses.append(status),
                )

                self.assertEqual(statuses, ["200 OK"])
                self.assertEqual(response, [path.encode()])
                self.assertEqual(load_count, 1)

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
