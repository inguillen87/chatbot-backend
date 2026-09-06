from __future__ import annotations

import json
import threading
import time
import unittest

from bootstrap_wsgi import LazyApplication


class LazyApplicationTests(unittest.TestCase):
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

    def test_background_warmup_starts_before_first_request_and_is_cached(self) -> None:
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
            startup_wait_seconds=0,
        )
        self.assertTrue(loader_started.wait(0.5))

        statuses: list[str] = []
        headers: list[tuple[str, str]] = []
        response = application(
            {"PATH_INFO": "/api/v2/demo/catalog", "REQUEST_METHOD": "GET"},
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

    def test_background_warmup_sheds_concurrent_reads_without_duplicate_loads(self) -> None:
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
            startup_wait_seconds=0,
        )
        self.assertTrue(loader_started.wait(0.5))

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
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started_at

        self.assertEqual(load_count, 1)
        self.assertEqual(statuses, ["503 Service Unavailable"] * 12)
        self.assertLess(elapsed, 0.5)
        release_loader.set()

    def test_only_one_request_waits_for_background_warmup(self) -> None:
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
            startup_wait_seconds=0.5,
        )
        self.assertTrue(loader_started.wait(0.5))

        waiting_statuses: list[str] = []
        waiting_response: list[list[bytes]] = []

        def waiting_request() -> None:
            waiting_response.append(
                application(
                    {"PATH_INFO": "/api/v2/demo/catalog", "REQUEST_METHOD": "GET"},
                    lambda status, headers: waiting_statuses.append(status),
                )
            )

        thread = threading.Thread(target=waiting_request)
        thread.start()
        deadline = time.monotonic() + 0.25
        while not application._waiter_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertTrue(application._waiter_lock.locked())

        shed_statuses: list[str] = []
        started_at = time.monotonic()
        shed_response = application(
            {"PATH_INFO": "/api/app/me/tenants", "REQUEST_METHOD": "GET"},
            lambda status, headers: shed_statuses.append(status),
        )
        shed_elapsed = time.monotonic() - started_at

        self.assertLess(shed_elapsed, 0.1)
        self.assertEqual(shed_statuses, ["503 Service Unavailable"])
        self.assertEqual(
            json.loads(b"".join(shed_response))["reason_code"],
            "application_initializing",
        )

        release_loader.set()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(waiting_statuses, ["200 OK"])
        self.assertEqual(waiting_response, [[b"/api/v2/demo/catalog"]])

    def test_background_warmup_fails_closed_without_exposing_exception(self) -> None:
        secret_detail = "private-runtime-secret"

        def loader():
            raise RuntimeError(secret_detail)

        application = LazyApplication(
            loader,
            background_warmup=True,
            startup_wait_seconds=0.1,
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
