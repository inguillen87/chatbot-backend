"""Lifecycle regression: prime only the Vercel worker, without executing requests."""
import os
from pathlib import Path
import runpy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bootstrap_wsgi import LazyApplication

ROOT = Path(__file__).resolve().parents[1]


def hook_for(env):
    with patch.dict(os.environ, env, clear=True):
        return runpy.run_path(str(ROOT / 'gunicorn.conf.py'))['post_worker_init']


class WorkerWarmupTests(unittest.TestCase):
    def test_render_never_starts_background_loader(self):
        warmup = Mock()
        with patch.dict(os.environ, {'RENDER': 'true'}, clear=True):
            hook_for({})(SimpleNamespace(wsgi=SimpleNamespace(start_warmup=warmup)))
        warmup.assert_not_called()

    def test_false_vercel_signal_does_not_start_loader(self):
        warmup = Mock()
        with patch.dict(os.environ, {'VERCEL': '0'}, clear=True):
            hook_for({})(SimpleNamespace(wsgi=SimpleNamespace(start_warmup=warmup)))
        warmup.assert_not_called()

    def test_normal_wsgi_app_does_not_need_a_warmup_method(self):
        with patch.dict(os.environ, {'VERCEL_ENV': 'preview'}, clear=True):
            hook_for({})(SimpleNamespace(wsgi=lambda env, start: []))

    def test_initialization_precedes_first_request_and_remains_single_flight(self):
        started, release = threading.Event(), threading.Event()
        loads, calls = [], []
        def canonical(env, start):
            calls.append(env['REQUEST_METHOD'])
            start('200 OK', [])
            return [b'ok']
        def loader():
            loads.append(True)
            started.set()
            release.wait(2)
            return canonical
        application = LazyApplication(loader, background_warmup=True,
            warmup_delay_seconds=0.01, safe_request_wait_seconds=0)
        worker = SimpleNamespace(wsgi=application)
        try:
            with patch.dict(os.environ, {'VERCEL_ENV': 'preview'}, clear=True):
                hook = hook_for({})
                before = time.monotonic()
                hook(worker)
                self.assertLess(time.monotonic() - before, 0.5)
                self.assertTrue(started.wait(1), 'warmup must not need an HTTP request')
                hook(worker)
                statuses = []
                application({'REQUEST_METHOD': 'POST'}, lambda status, headers: statuses.append(status))
                self.assertEqual(statuses, ['503 Service Unavailable'])
                self.assertEqual(calls, [])
                self.assertEqual(len(loads), 1)
        finally:
            release.set()
            self.assertTrue(application._ready.wait(2))
        application({'REQUEST_METHOD': 'GET'}, lambda status, headers: None)
        self.assertEqual(calls, ['GET'])
        self.assertEqual(len(loads), 1)


if __name__ == '__main__':
    unittest.main()
