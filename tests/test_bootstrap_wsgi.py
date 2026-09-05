from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
