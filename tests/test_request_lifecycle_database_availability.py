"""Full-app request-lifecycle errors with synthetic storage seams and no SQL.

Run in the disposable profile_acceptance_runtime.prepare_process process. The
real Flask request context, exception dispatch, registered routes and handlers
are exercised; opening/saving a session and rollback are controlled boundaries.
"""
from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DisconnectionError, IntegrityError, OperationalError, TimeoutError
from werkzeug.exceptions import Forbidden, InternalServerError, Unauthorized


class RequestLifecycleDatabaseAvailabilityTest(unittest.TestCase):
    finalization_observations = []

    @classmethod
    def setUpClass(cls):
        from app import create_app, db
        from config import TestingConfig

        cls.sql_attempts = []

        def reject_sql(*args):
            cls.sql_attempts.append('forbidden_sql')
            raise AssertionError('Lifecycle availability fixture must not execute SQL')

        cls.reject_sql = reject_sql
        event.listen(Engine, 'before_cursor_execute', cls.reject_sql)
        cls.addClassCleanup(event.remove, Engine, 'before_cursor_execute', cls.reject_sql)

        class OfflineConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False
            SKIP_INIT_TENANTS = True
            PROPAGATE_EXCEPTIONS = False
            CUTOVER_WRITER_FENCE_ENABLED = False
            CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED = False
            CORS_CREDENTIALS_ALLOWED_ORIGINS = ['https://panel.example.invalid']

        with patch.object(db, 'create_all'):
            cls.app = create_app(OfflineConfig)
        cls.db = db

        @cls.app.route('/__test/lifecycle-error')
        def lifecycle_error():
            raise cls.app.config['SYNTHETIC_LIFECYCLE_ERROR']

        cls.ask_endpoint = next(rule.endpoint for rule in cls.app.url_map.iter_rules()
                                if rule.rule == '/api/ask/pyme' and 'POST' in rule.methods)

    def setUp(self):
        import middleware.tenant_context as tenant_context

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app.config.update(CUTOVER_WRITER_FENCE_ENABLED=False,
                               CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=False)
        self.rollback = self.stack.enter_context(patch.object(self.db.session, 'rollback'))
        self.stack.enter_context(patch.object(self.app.session_interface, 'save_session', return_value=None))
        self.tenant = self.stack.enter_context(patch.object(
            tenant_context, '_resolve_tenant_profile', return_value=None))
        self.ask = Mock(side_effect=AssertionError('Session failure must not dispatch the ask view'))
        self.stack.enter_context(patch.dict(self.app.view_functions, {self.ask_endpoint: self.ask}))
        self.client = self.app.test_client()
        self.addCleanup(self.assertEqual, self.sql_attempts, [])

    @staticmethod
    def availability_errors():
        return (
            OperationalError('SELECT synthetic', {}, Exception('PRIVATE_LIFECYCLE_DIAGNOSTIC')),
            DisconnectionError('PRIVATE_LIFECYCLE_DIAGNOSTIC'),
            TimeoutError('PRIVATE_LIFECYCLE_DIAGNOSTIC'),
        )

    def session_failure(self, error, headers=None):
        with patch.object(self.app.session_interface, 'open_session', side_effect=error) as opener:
            response = self.client.post('/api/ask/pyme', headers=headers or {}, json={
                'pregunta': '__INIT__', 'action': 'initial_greeting',
                'tipo_chat': 'pyme', 'session_id': 'synthetic-lifecycle-only',
            })
        opener.assert_called_once()
        self.ask.assert_not_called()
        self.tenant.assert_not_called()
        return response

    def assert_unavailable(self, response, request_id=None):
        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload['contract_version'], 'shared.error.v1')
        self.assertEqual(payload['status_code'], 503)
        self.assertEqual(payload['reason_code'], 'database_unavailable')
        self.assertIs(payload['retryable'], False)
        self.assertEqual(payload['action_hint'], 'retry_later')
        self.assertEqual(payload['message'],
                         'El servicio no está disponible temporalmente. Intentá nuevamente más tarde.')
        self.assertEqual(payload['error'], {'code': 503, 'message': payload['message']})
        self.assertEqual(payload['detail'], payload['message'])
        self.assertTrue(payload['request_id'])
        self.assertEqual(payload['request_id'], response.headers['X-Request-Id'])
        if request_id is not None:
            self.assertEqual(payload['request_id'], request_id)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Retry-After', response.headers)
        self.assertNotIn('X-Chatboc-Bootstrap', response.headers)
        self.assertNotIn('request_dispatched', payload)
        for private_fragment in ('PRIVATE_LIFECYCLE_DIAGNOSTIC', 'SELECT synthetic',
                                 'OperationalError', 'DisconnectionError', 'TimeoutError'):
            self.assertNotIn(private_fragment, response.get_data(as_text=True))

    def assert_server_error(self, response):
        self.assertEqual(response.status_code, 500)
        payload = response.get_json()
        self.assertEqual(payload['contract_version'], 'shared.error.v1')
        self.assertEqual(payload['reason_code'], 'server_error')
        self.assertEqual(payload['message'], 'Internal server error')
        self.assertIs(payload['retryable'], True)
        self.assertNotIn('PRIVATE_LIFECYCLE_DIAGNOSTIC', response.get_data(as_text=True))

    def test_real_flask_session_open_wraps_typed_availability_failures(self):
        for error in self.availability_errors():
            with self.subTest(error_type=type(error).__name__):
                self.assert_unavailable(self.session_failure(error))

    def test_registered_rubros_get_obeys_the_same_session_open_boundary(self):
        endpoint = next(rule.endpoint for rule in self.app.url_map.iter_rules()
                        if rule.rule == '/api/rubros/' and 'GET' in rule.methods)
        view = Mock(side_effect=AssertionError('Session failure must not dispatch rubros'))
        with patch.dict(self.app.view_functions, {endpoint: view}), \
                patch.object(self.app.session_interface, 'open_session',
                             side_effect=self.availability_errors()[0]) as opener:
            response = self.client.get('/api/rubros/',
                                       headers={'X-Request-Id': 'synthetic-rubros-lifecycle'})
        self.assert_unavailable(response, 'synthetic-rubros-lifecycle')
        opener.assert_called_once()
        view.assert_not_called()
        self.tenant.assert_not_called()

    def test_request_id_is_preserved_when_session_open_fails(self):
        response = self.session_failure(self.availability_errors()[0],
                                        {'X-Request-Id': 'synthetic-lifecycle-request'})
        self.assert_unavailable(response, 'synthetic-lifecycle-request')

    def test_correlation_id_is_preserved_when_request_id_is_absent(self):
        response = self.session_failure(self.availability_errors()[1],
                                        {'X-Correlation-Id': 'synthetic-lifecycle-correlation'})
        self.assert_unavailable(response, 'synthetic-lifecycle-correlation')

    def test_generated_request_ids_are_distinct_per_failed_request(self):
        first = self.session_failure(self.availability_errors()[2])
        second = self.session_failure(self.availability_errors()[2])
        self.assert_unavailable(first)
        self.assert_unavailable(second)
        self.assertNotEqual(first.get_json()['request_id'], second.get_json()['request_id'])

    def test_rollback_failure_does_not_replace_availability_contract(self):
        self.rollback.side_effect = TimeoutError('PRIVATE_LIFECYCLE_DIAGNOSTIC')
        self.assert_unavailable(self.session_failure(self.availability_errors()[0]))

    def test_text_resembling_database_failure_remains_server_error(self):
        self.assert_server_error(self.session_failure(
            RuntimeError('OperationalError database quota PRIVATE_LIFECYCLE_DIAGNOSTIC')))

    def test_non_availability_sqlalchemy_error_remains_server_error(self):
        self.assert_server_error(self.session_failure(
            IntegrityError('SELECT synthetic', {}, Exception('PRIVATE_LIFECYCLE_DIAGNOSTIC'))))

    def test_typed_cause_of_unrelated_original_exception_is_not_reclassified(self):
        error = RuntimeError('PRIVATE_LIFECYCLE_DIAGNOSTIC')
        error.__cause__ = self.availability_errors()[0]
        self.assert_server_error(self.session_failure(error))

    def test_explicit_internal_server_error_without_original_remains_500(self):
        self.app.config['SYNTHETIC_LIFECYCLE_ERROR'] = InternalServerError()
        self.assert_server_error(self.client.get('/__test/lifecycle-error'))

    def test_only_typed_original_exception_is_unwrapped_by_500_handler(self):
        for error in self.availability_errors():
            with self.subTest(error_type=type(error).__name__):
                self.app.config['SYNTHETIC_LIFECYCLE_ERROR'] = InternalServerError(original_exception=error)
                self.assert_unavailable(self.client.get('/__test/lifecycle-error'))

    def test_original_route_availability_handler_retains_same_contract(self):
        self.app.config['SYNTHETIC_LIFECYCLE_ERROR'] = self.availability_errors()[0]
        self.assert_unavailable(self.client.get('/__test/lifecycle-error'))

    def test_http_authentication_and_authorization_denials_are_not_availability(self):
        for error in (Unauthorized(), Forbidden()):
            with self.subTest(status=error.code):
                self.app.config['SYNTHETIC_LIFECYCLE_ERROR'] = error
                response = self.client.get('/__test/lifecycle-error')
                self.assertEqual(response.status_code, error.code)
                self.assertEqual(response.get_json()['reason_code'], 'http_error')

    def test_global_cutover_fence_preserves_its_distinct_contract(self):
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        response = self.client.post('/api/ask/pyme', json={'pregunta': '__INIT__'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['reason_code'], 'cutover_writer_fence_enabled')
        self.assertIs(response.get_json()['retryable'], True)
        self.assertEqual(response.headers['Retry-After'], '60')
        self.ask.assert_not_called()
        self.tenant.assert_not_called()

    def test_global_authority_denial_preserves_its_distinct_contract(self):
        import middleware.cutover_writer_fence as fence
        from services.global_writer_authority import GlobalWriterAuthorityDecision

        decision = GlobalWriterAuthorityDecision(
            allowed=False, enabled=True,
            reason_code='global_writer_authority_control_database_unavailable')
        with patch.object(fence, 'evaluate_global_writer_authority', return_value=decision):
            response = self.client.get('/api/public/widget-config')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['contract_version'], 'cutover.global_writer_authority.v1')
        self.assertEqual(response.get_json()['reason_code'], decision.reason_code)
        self.assertIs(response.get_json()['retryable'], True)
        self.assertEqual(response.headers['Retry-After'], '60')
        self.tenant.assert_not_called()

    def test_repeated_session_save_failure_retains_neutral_availability_response(self):
        endpoint = next(rule.endpoint for rule in self.app.url_map.iter_rules()
                        if rule.rule == '/api/rubros/' and 'GET' in rule.methods)
        for error in self.availability_errors():
            with self.subTest(error_type=type(error).__name__):
                calls = []

                def view():
                    calls.append('view')
                    return {'synthetic': 'initial-success-before-save'}
                with patch.dict(self.app.view_functions, {endpoint: view}), \
                        patch.object(self.app.session_interface, 'save_session', side_effect=error) as save, \
                        patch.object(self.app.logger, 'exception', wraps=self.app.logger.exception) as finalizing_log:
                    response = self.client.get('/api/rubros/', headers={
                        'Origin': 'https://panel.example.invalid',
                        'X-Request-Id': 'synthetic-save-lifecycle',
                    })
                self.finalization_observations.append({
                    'boundary': 'save_session', 'error_type': type(error).__name__,
                    'status': response.status_code, 'reason_code': response.get_json()['reason_code'],
                    'calls': save.call_count, 'finalizing_failure_logs': finalizing_log.call_count,
                    'access_control_allow_origin': response.headers.get('Access-Control-Allow-Origin'),
                })
                self.assert_unavailable(response, 'synthetic-save-lifecycle')
                self.assertEqual(calls, ['view'])
                self.assertEqual(save.call_count, 2)
                self.assertEqual(response.headers.get('Access-Control-Allow-Origin'),
                                 'https://panel.example.invalid')
                self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
                self.assertIn('X-Request-Id', response.headers['Access-Control-Expose-Headers'])
                finalizing_log.assert_called_once_with(
                    'Request finalizing failed with an error while handling an error')

    def test_repeated_after_request_failure_retains_neutral_availability_response(self):
        endpoint = next(rule.endpoint for rule in self.app.url_map.iter_rules()
                        if rule.rule == '/api/rubros/' and 'GET' in rule.methods)
        for error in self.availability_errors():
            with self.subTest(error_type=type(error).__name__):
                calls = []

                def view():
                    calls.append('view')
                    return {'synthetic': 'initial-success-before-after-request'}
                callback = Mock(side_effect=error)
                callbacks = [*self.app.after_request_funcs.get(None, ()), callback]
                with patch.dict(self.app.view_functions, {endpoint: view}), \
                        patch.dict(self.app.after_request_funcs, {None: callbacks}), \
                        patch.object(self.app.logger, 'exception', wraps=self.app.logger.exception) as finalizing_log:
                    response = self.client.get('/api/rubros/', headers={
                        'Origin': 'https://panel.example.invalid',
                        'X-Request-Id': 'synthetic-after-request-lifecycle',
                    })
                self.finalization_observations.append({
                    'boundary': 'after_request_first_callback', 'error_type': type(error).__name__,
                    'status': response.status_code, 'reason_code': response.get_json()['reason_code'],
                    'calls': callback.call_count, 'finalizing_failure_logs': finalizing_log.call_count,
                    'access_control_allow_origin': response.headers.get('Access-Control-Allow-Origin'),
                })
                self.assert_unavailable(response, 'synthetic-after-request-lifecycle')
                self.assertEqual(calls, ['view'])
                self.assertEqual(callback.call_count, 2)
                finalizing_log.assert_called_once_with(
                    'Request finalizing failed with an error while handling an error')
