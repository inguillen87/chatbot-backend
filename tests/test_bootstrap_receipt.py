"""Explicit pre-dispatch receipt, without changing when business handlers run."""
import json
import threading
import unittest
from bootstrap_wsgi import LazyApplication

class StartupReceiptTests(unittest.TestCase):
    def test_receipt_is_only_emitted_before_handler_dispatch(self):
        for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                release = threading.Event()
                dispatches = []
                def handler(environ, start_response):
                    dispatches.append(method)
                    start_response('200 OK', [])
                    return [b'ok']
                def load():
                    release.wait(2)
                    return handler
                app = LazyApplication(load, background_warmup=True,
                    warmup_delay_seconds=0, safe_request_wait_seconds=0)
                try:
                    response = app({'REQUEST_METHOD': method}, lambda *args: None)
                    payload = json.loads(b''.join(response))
                    self.assertIs(payload['request_dispatched'], False)
                    self.assertIs(payload['retryable'], True)
                    self.assertEqual(dispatches, [])
                finally:
                    release.set()
                    self.assertTrue(app._ready.wait(1))
                self.assertEqual(dispatches, [])
                self.assertEqual(app({'REQUEST_METHOD': method}, lambda *args: None), [b'ok'])
                self.assertEqual(dispatches, [method])

    def test_failed_loader_is_not_recoverable(self):
        app = LazyApplication(lambda: None)
        payload = json.loads(b''.join(app._bootstrap_response({}, lambda *args: None, failed=True)))
        self.assertIs(payload['request_dispatched'], False)
        self.assertIs(payload['retryable'], False)
        self.assertEqual(payload['reason_code'], 'application_initialization_failed')

if __name__ == '__main__':
    unittest.main(verbosity=2)
