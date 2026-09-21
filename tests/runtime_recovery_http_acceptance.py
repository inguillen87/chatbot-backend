"""Dedicated process: real create_app, HTTP, sessions, models and middleware.
Synthetic accounts/database only. No deployed environment or provider acceptance.
"""
from tests.profile_acceptance_runtime import prepare_process, create_disposable_app

if __name__ == '__main__':
    prepare_process()

import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor, Request
from urllib.error import HTTPError
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
COPY_PATH = '/api/config/runtime-recovery'
VERSION = 'a' * 40


class BrowserSession:
    def __init__(self, origin):
        self.origin = origin
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def request(self, method, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.origin + path, data=body, method=method,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = self.opener.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            content = json.loads(raw) if raw and 'application/json' in response.headers.get('Content-Type', '') else None
            return response.status, content, response.headers


class RecoveryApplicationServer:
    """Fault injection exists only in this test harness, never in production routes."""
    def __init__(self):
        from flask import has_request_context, request, jsonify
        from sqlalchemy import event
        from werkzeug.serving import make_server
        from database import db
        self.directory = tempfile.TemporaryDirectory(prefix='chatboc-recovery-http-')
        self.app, self.accounts, self.password = create_disposable_app(self.directory.name)
        self.app.config['BACKEND_VERSION'] = VERSION
        self.app.config['FRONTEND_VERSION'] = 'acceptance-only'
        self.control_token = secrets.token_urlsafe(32)
        self.mode = 'normal'
        self.blocked_sql = []
        self.expected = json.loads((ROOT / 'config/runtime_recovery_ui.json').read_text(encoding='utf-8'))
        with self.app.app_context():
            self.engine = db.engine

        def sql_guard(conn, cursor, statement, parameters, context, executemany):
            if self.mode == 'database-unavailable':
                path = request.path if has_request_context() else 'outside-request'
                self.blocked_sql.append(path)
                raise RuntimeError('Acceptance-only database outage')
        self.sql_guard = sql_guard
        event.listen(self.engine, 'before_cursor_execute', self.sql_guard)

        @self.app.before_request
        def inject_recovery_fault():
            if request.path != '/api/version':
                return None
            if self.mode == 'unavailable':
                return jsonify({'error': 'acceptance_service_unavailable'}), 503
            if self.mode == 'slow':
                time.sleep(3)
            return None

        @self.app.post('/__acceptance__/control')
        def acceptance_control():
            if request.remote_addr != '127.0.0.1' or not secrets.compare_digest(
                    request.headers.get('X-Acceptance-Token', ''), self.control_token):
                return jsonify({'error': 'forbidden'}), 403
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {'mode'} or data['mode'] not in {
                    'normal', 'unavailable', 'slow', 'mismatch', 'database-unavailable'}:
                return jsonify({'error': 'invalid_fault_mode'}), 400
            self.mode = data['mode']
            self.app.config['BACKEND_VERSION'] = 'b' * 40 if self.mode == 'mismatch' else VERSION
            return jsonify({'test_control': True})

        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        from sqlalchemy import event
        from database import db
        self.mode = 'normal'
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        event.remove(self.engine, 'before_cursor_execute', self.sql_guard)
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.directory.cleanup()


class RuntimeRecoveryFullAppHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = RecoveryApplicationServer()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()

    def setUp(self):
        self.runtime.mode = 'normal'
        self.runtime.app.config['BACKEND_VERSION'] = VERSION
        self.runtime.blocked_sql.clear()

    def tearDown(self):
        self.runtime.mode = 'normal'

    def browser(self):
        return BrowserSession(self.runtime.origin)

    def login(self, account='acceptance-a'):
        browser = self.browser()
        status, body, _ = browser.request('POST', '/auth/login', {
            'email': self.runtime.accounts[account]['email'], 'password': self.runtime.password})
        self.assertEqual(status, 200, 'Original password login failed')
        self.assertTrue(body.get('token'), 'Original login did not issue its signed token')
        return browser

    def test_anonymous_copy_uses_full_registered_application(self):
        status, body, headers = self.browser().request('GET', COPY_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.runtime.expected)
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertNotIn('Set-Cookie', headers)

    def test_hints_do_not_change_platform_copy(self):
        status, body, _ = self.browser().request('GET', COPY_PATH + '?tenant_slug=acceptance-b&tenant_id=2',
            headers={'X-Tenant-Slug': 'acceptance-b', 'X-Widget-Token': 'non-secret-test-hint'})
        self.assertEqual(status, 200)
        self.assertEqual(body, self.runtime.expected)

    def test_copy_remains_available_when_sql_execution_is_denied(self):
        self.runtime.mode = 'database-unavailable'
        status, body, _ = self.browser().request('GET', COPY_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.runtime.expected)
        self.assertEqual(self.runtime.blocked_sql, [], 'Global request chain attempted SQL')

    def test_head_and_hints_also_avoid_sql_during_outage(self):
        self.runtime.mode = 'database-unavailable'
        status, _, headers = self.browser().request('HEAD', COPY_PATH + '?tenant_slug=acceptance-a',
            headers={'X-Tenant-Slug': 'acceptance-a'})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertEqual(self.runtime.blocked_sql, [])

    def test_same_original_session_survives_public_copy_read(self):
        browser = self.login()
        path = '/api/me?tenant_slug=acceptance-a'
        self.assertEqual(browser.request('GET', path)[0], 200)
        self.assertEqual(browser.request('GET', COPY_PATH)[1], self.runtime.expected)
        self.assertEqual(browser.request('GET', path)[0], 200)

    def test_anonymous_copy_does_not_authorize_private_config(self):
        browser = self.browser()
        self.assertEqual(browser.request('GET', COPY_PATH)[0], 200)
        status, _, _ = browser.request('GET', '/api/admin/tenants/acceptance-a/config')
        self.assertEqual(status, 401)

    def test_wrong_password_is_not_repaired_by_public_copy(self):
        browser = self.browser()
        self.assertEqual(browser.request('GET', COPY_PATH)[0], 200)
        status, _, _ = browser.request('POST', '/auth/login', {
            'email': self.runtime.accounts['acceptance-a']['email'], 'password': 'wrong-acceptance-password'})
        self.assertEqual(status, 401)

    def test_foreign_organization_still_cannot_read_private_config(self):
        browser = self.login('acceptance-b')
        self.assertEqual(browser.request('GET', COPY_PATH)[0], 200)
        status, _, _ = browser.request('GET', '/api/admin/tenants/acceptance-a/config')
        self.assertIn(status, (403, 404))

    def test_missing_configuration_fails_without_leaking_paths(self):
        with patch('routes.config._load_runtime_recovery_ui', side_effect=OSError('private-acceptance-path')):
            status, body, headers = self.browser().request('GET', COPY_PATH)
        self.assertEqual(status, 503)
        self.assertEqual(body, {'error': 'runtime_recovery_config_unavailable'})
        self.assertEqual(headers.get('Cache-Control'), 'no-store')

    def test_version_response_remains_original(self):
        status, body, _ = self.browser().request('GET', '/api/version')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'backend': VERSION, 'frontend': 'acceptance-only'})

    def test_write_methods_are_denied_through_real_global_hooks(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                status, _, _ = self.browser().request(method, COPY_PATH, {'unexpected': True})
                self.assertIn(status, (400, 401, 403, 405))
        route = next(rule for rule in self.runtime.app.url_map.iter_rules()
            if rule.endpoint == 'config_bp.get_runtime_recovery_ui')
        self.assertEqual(route.methods, {'GET', 'HEAD', 'OPTIONS'})

    def test_test_control_is_not_open_to_ordinary_http_clients(self):
        status, _, _ = self.browser().request('POST', '/__acceptance__/control', {'mode': 'unavailable'})
        self.assertEqual(status, 403)
        self.assertEqual(self.runtime.mode, 'normal')


if __name__ == '__main__':
    unittest.main(verbosity=2)
