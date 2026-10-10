"""Minimal WSGI fixture for the real Linux Gunicorn lifecycle gate."""
import json
import os
from pathlib import Path
import time

from bootstrap_wsgi import LazyApplication


def load_application():
    # This marker must appear before the test sends its first HTTP request.
    Path(os.environ['WARMUP_SMOKE_MARKER']).write_text('loader-started', encoding='utf-8')
    time.sleep(0.3)
    def canonical(environ, start_response):
        body = json.dumps({'ok': True, 'method': environ['REQUEST_METHOD']}).encode()
        start_response('200 OK', [('Content-Type', 'application/json'),
                                  ('Content-Length', str(len(body)))])
        return [body]
    return canonical


application = LazyApplication(load_application, background_warmup=True,
                              safe_request_wait_seconds=1.0)
