"""The original signed webhook may wait for startup, never replay or pre-ACK."""
from io import BytesIO
import json
import os
import threading
import unittest
from unittest.mock import patch

import bootstrap_wsgi as bootstrap


class ProductionWhatsAppWebhookBootstrapTests(unittest.TestCase):
    def production_app(self, loader, seconds=.25):
        with patch.dict(os.environ, {'VERCEL_ENV': 'production'}, clear=True), patch.object(
            bootstrap, '_PRODUCTION_WHATSAPP_WEBHOOK_WAIT_SECONDS', seconds, create=True,
        ):
            return bootstrap.LazyApplication(loader, background_warmup=True,
                                              warmup_delay_seconds=0)

    def webhook_environ(self, body=b'synthetic-original-body', **overrides):
        environ = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/webhook/whatsapp',
                   'CONTENT_TYPE': 'application/x-www-form-urlencoded',
                   'CONTENT_LENGTH': str(len(body)), 'wsgi.input': BytesIO(body),
                   'HTTP_X_TWILIO_SIGNATURE': 'synthetic-original-signature'}
        environ.update(overrides)
        return environ

    def test_cold_original_webhook_waits_then_dispatches_original_environ_once(self):
        started, release = threading.Event(), threading.Event()
        observed, statuses, result = [], [], []

        def target(environ, start_response):
            observed.append((environ, environ['wsgi.input'].read(),
                             environ['HTTP_X_TWILIO_SIGNATURE']))
            start_response('200 OK', [('X-Canonical-Fixture', '1')])
            return [b'canonical-response']

        def loader():
            started.set()
            release.wait(1)
            return target

        application = self.production_app(loader)
        environ = self.webhook_environ(QUERY_STRING='opaque-existing-query=1')
        headers = []
        request = threading.Thread(target=lambda: result.extend(application(environ,
            lambda status, values: (statuses.append(status), headers.extend(values)))))
        request.start()
        self.assertTrue(started.wait(.5))
        self.assertEqual(observed, [])
        self.assertEqual(environ['wsgi.input'].tell(), 0)
        release.set()
        request.join(1)
        self.assertFalse(request.is_alive())
        self.assertEqual(statuses, ['200 OK'])
        self.assertEqual(result, [b'canonical-response'])
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0][0], environ)
        self.assertEqual(observed[0][1:],
                         (b'synthetic-original-body', 'synthetic-original-signature'))
        self.assertEqual(dict(headers)['X-Canonical-Fixture'], '1')

    def test_timeout_preserves_503_and_never_dispatches_after_loading_completes(self):
        release = threading.Event()
        dispatched = []

        def target(environ, start_response):
            dispatched.append(environ)
            start_response('200 OK', [])
            return [b'unexpected']

        def loader():
            release.wait(1)
            return target

        application = self.production_app(loader, seconds=.02)
        environ = self.webhook_environ()
        statuses, headers = [], []
        response = application(environ,
            lambda status, values: (statuses.append(status), headers.extend(values)))
        payload = json.loads(b''.join(response))
        self.assertEqual(statuses, ['503 Service Unavailable'])
        self.assertEqual(payload['contract_version'], 'chatboc.bootstrap.v1')
        self.assertEqual(payload['reason_code'], 'application_initializing')
        self.assertFalse(payload['request_dispatched'])
        self.assertTrue(payload['retryable'])
        self.assertEqual(dict(headers)['X-Chatboc-Bootstrap'], 'initializing')
        self.assertEqual(dict(headers)['Retry-After'], '2')
        self.assertEqual(dict(headers)['Cache-Control'], 'no-store')
        self.assertEqual(environ['wsgi.input'].tell(), 0)
        self.assertEqual(dispatched, [])
        release.set()
        self.assertTrue(application._ready.wait(.5))
        self.assertEqual(dispatched, [])
        self.assertEqual(environ['wsgi.input'].tell(), 0)

    def test_failed_loader_keeps_webhook_closed_without_exposing_exception(self):
        def loader():
            raise RuntimeError('synthetic-private-loader-detail')

        application = self.production_app(loader)
        environ = self.webhook_environ()
        statuses = []
        response = b''.join(application(environ,
            lambda status, values: statuses.append(status)))
        payload = json.loads(response)
        self.assertEqual(statuses, ['503 Service Unavailable'])
        self.assertEqual(payload['reason_code'], 'application_initialization_failed')
        self.assertFalse(payload['request_dispatched'])
        self.assertFalse(payload['retryable'])
        self.assertEqual(environ['wsgi.input'].tell(), 0)
        self.assertNotIn(b'synthetic-private-loader-detail', response)

    def test_concurrent_original_webhooks_share_loader_and_dispatch_each_once(self):
        started, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        loads, observed, statuses = [], [], []

        def target(environ, start_response):
            with lock:
                observed.append((environ['fixture_request_id'], environ['wsgi.input'].read()))
            start_response('200 OK', [])
            return [b'accepted']

        def loader():
            loads.append(1)
            started.set()
            release.wait(1)
            return target

        application = self.production_app(loader, seconds=.5)

        def invoke(index):
            def capture(status, values):
                with lock:
                    statuses.append(status)
            application(self.webhook_environ(str(index).encode(), fixture_request_id=index), capture)

        threads = [threading.Thread(target=invoke, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(.5))
        self.assertEqual(observed, [])
        release.set()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(loads, [1])
        self.assertEqual(statuses, ['200 OK'] * 12)
        self.assertEqual(sorted(observed), [(index, str(index).encode()) for index in range(12)])

    def test_only_exact_post_webhook_path_receives_new_wait(self):
        for method, path in [
            ('PUT', '/webhook/whatsapp'), ('PATCH', '/webhook/whatsapp'),
            ('DELETE', '/webhook/whatsapp'), ('POST', '/webhook/whatsapp/'),
            ('POST', '/webhook/whatsapp/extra'), ('POST', '/api/webhook/whatsapp'),
            ('POST', '/WEBHOOK/WHATSAPP'), ('POST', '/webhook/%77hatsapp'),
            ('POST', '/twilio/whatsapp/status'), ('POST', '/webhook/meta/tdf-sandbox'),
            ('POST', '/api/orders'), ('POST', '/api/auth/password/reset/request'),
        ]:
            with self.subTest(method=method, path=path):
                release = threading.Event()

                def loader():
                    release.wait(1)
                    return lambda environ, start_response: [b'unexpected']

                application = self.production_app(loader)
                environ = self.webhook_environ(REQUEST_METHOD=method, PATH_INFO=path)
                with patch.object(application._ready, 'wait',
                                  side_effect=AssertionError('Unrelated mutations must not wait')):
                    response = application(environ, lambda status, values: None)
                self.assertFalse(json.loads(b''.join(response))['request_dispatched'])
                self.assertEqual(environ['wsgi.input'].tell(), 0)
                release.set()
                self.assertTrue(application._ready.wait(.5))

    def test_webhook_budget_is_fixed_and_independent_of_read_login_or_env_overrides(self):
        self.assertEqual(bootstrap._PRODUCTION_WHATSAPP_WEBHOOK_WAIT_SECONDS, 5.0)
        release = threading.Event()

        def loader():
            release.wait(1)
            return lambda environ, start_response: [b'unexpected']

        with patch.dict(os.environ, {'VERCEL_ENV': 'production',
            'VERCEL_WSGI_SAFE_REQUEST_WAIT_SECONDS': '0',
            'VERCEL_WSGI_MUTATION_REQUEST_WAIT_SECONDS': '999'}, clear=True):
            application = bootstrap.LazyApplication(loader, background_warmup=True,
                warmup_delay_seconds=0, safe_request_wait_seconds=0,
                mutation_request_wait_seconds=999)
        self.assertEqual(application._production_whatsapp_webhook_wait_seconds, 5.0)
        self.assertEqual(application._production_login_wait_seconds, 0)
        self.assertEqual(application._mutation_request_wait_seconds, 0)
        with patch.object(application._ready, 'wait', return_value=False) as wait:
            response = application(self.webhook_environ(), lambda status, values: None)
        wait.assert_called_once_with(5.0)
        self.assertFalse(json.loads(b''.join(response))['request_dispatched'])
        release.set()
        self.assertTrue(application._ready.wait(.5))

    def test_other_runtime_environments_keep_existing_fail_fast_policy(self):
        for environment in ('preview', 'development', ''):
            with self.subTest(environment=environment), patch.dict(
                os.environ, {'VERCEL_ENV': environment}, clear=True,
            ):
                release = threading.Event()

                def loader():
                    release.wait(1)
                    return lambda environ, start_response: [b'unexpected']

                application = bootstrap.LazyApplication(loader, background_warmup=True,
                                                        warmup_delay_seconds=0)
                environ = self.webhook_environ()
                with patch.object(application._ready, 'wait',
                                  side_effect=AssertionError('Existing runtime policy changed')):
                    response = application(environ, lambda status, values: None)
                self.assertFalse(json.loads(b''.join(response))['request_dispatched'])
                self.assertEqual(environ['wsgi.input'].tell(), 0)
                release.set()
                self.assertTrue(application._ready.wait(.5))

    def test_warm_application_dispatches_once_without_wait(self):
        observed = []

        def target(environ, start_response):
            observed.append(environ)
            start_response('200 OK', [])
            return [b'accepted']

        application = self.production_app(lambda: target)
        application._load_and_cache()
        environ = self.webhook_environ()
        with patch.object(application._ready, 'wait', side_effect=AssertionError('Warm app waited')):
            self.assertEqual(application(environ, lambda status, values: None), [b'accepted'])
        self.assertEqual(observed, [environ])


if __name__ == '__main__':
    unittest.main(verbosity=2)
