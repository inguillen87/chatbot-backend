"""Registered rubros HTTP routes with controlled ORM seams; no SQL or network.

Run in tests.profile_acceptance_runtime.prepare_process's disposable process.
The Flask app, production blueprints and error handlers are application code.
"""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DisconnectionError, IntegrityError, OperationalError, TimeoutError


class RubrosDatabaseAvailabilityTest(unittest.TestCase):
    BASES = ('/rubros', '/rubros/', '/api/rubros', '/api/rubros/')

    @classmethod
    def setUpClass(cls):
        from app import create_app, db
        from config import TestingConfig

        cls.sql_attempts = []

        def reject_sql(*args):
            cls.sql_attempts.append('forbidden_sql')
            raise AssertionError('Rubros availability fixture must not execute SQL')

        cls.reject_sql = reject_sql
        event.listen(Engine, 'before_cursor_execute', cls.reject_sql)
        cls.addClassCleanup(event.remove, Engine, 'before_cursor_execute', cls.reject_sql)

        class OfflineConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False
            SKIP_INIT_TENANTS = True
            PROPAGATE_EXCEPTIONS = False
            ENABLE_DEMO_MODE = False
            CUTOVER_WRITER_FENCE_ENABLED = False
            CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED = False

        with patch.object(db, 'create_all'):
            cls.app = create_app(OfflineConfig)
        cls.db = db

    def setUp(self):
        import middleware.tenant_context as tenant_context
        import routes.rubros as rubros_routes

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app.config['ENABLE_DEMO_MODE'] = False
        self.stack.enter_context(patch.object(tenant_context, '_resolve_tenant_profile', return_value=None))
        self.stack.enter_context(patch.object(self.app.session_interface, 'save_session', return_value=None))
        self.rollback = self.stack.enter_context(patch.object(self.db.session, 'rollback'))
        self.query = Mock()
        self.query.filter_by.return_value = self.query
        self.query.order_by.return_value = self.query
        self.query.all.return_value = []
        model = SimpleNamespace(query=self.query, nombre=Mock())
        self.stack.enter_context(patch.object(rubros_routes, 'Rubro', model))
        self.demo = self.stack.enter_context(patch.object(rubros_routes, 'load_demo_rubros', return_value=[]))
        self.client = self.app.test_client()
        self.addCleanup(self.assertEqual, self.sql_attempts, [])

    @staticmethod
    def availability_errors():
        return (
            OperationalError('SYNTHETIC_SQL_SENTINEL', {'private': 'PRIVATE_RUBROS_VALUE'},
                             Exception('PRIVATE_RUBROS_DIAGNOSTIC')),
            DisconnectionError('PRIVATE_RUBROS_DIAGNOSTIC'),
            TimeoutError('PRIVATE_RUBROS_DIAGNOSTIC'),
        )

    def assert_unavailable(self, response, request_id):
        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertEqual(body['contract_version'], 'shared.error.v1')
        self.assertEqual(body['status_code'], 503)
        self.assertEqual(body['reason_code'], 'database_unavailable')
        self.assertIs(body['retryable'], False)
        self.assertEqual(body['action_hint'], 'retry_later')
        self.assertEqual(body['request_id'], request_id)
        self.assertEqual(response.headers['X-Request-Id'], request_id)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Retry-After', response.headers)
        self.assertNotIn('X-Chatboc-Bootstrap', response.headers)
        self.assertNotIn('request_dispatched', body)
        for fragment in ('PRIVATE_RUBROS', 'SYNTHETIC_SQL_SENTINEL', 'OperationalError',
                         'DisconnectionError', 'TimeoutError'):
            self.assertNotIn(fragment, response.get_data(as_text=True))

    def check_query_failure_for_routes(self, error):
        self.query.all.side_effect = error
        for base in self.BASES:
            for suffix in ('', '?format=tree'):
                with self.subTest(path=base + suffix):
                    response = self.client.get(base + suffix, headers={'X-Request-Id': 'rubros-synthetic-request'})
                    self.assert_unavailable(response, 'rubros-synthetic-request')
        self.query.filter_by.assert_called_with(es_publico=True)
        self.demo.assert_not_called()

    def test_operational_error_from_query_reaches_shared_handler(self):
        self.check_query_failure_for_routes(self.availability_errors()[0])

    def test_disconnection_from_query_reaches_shared_handler(self):
        self.check_query_failure_for_routes(self.availability_errors()[1])

    def test_pool_timeout_from_query_reaches_shared_handler(self):
        self.check_query_failure_for_routes(self.availability_errors()[2])

    def test_demo_registry_typed_failure_reaches_shared_handler(self):
        self.app.config['ENABLE_DEMO_MODE'] = True
        for error in self.availability_errors():
            with self.subTest(error_type=type(error).__name__):
                self.demo.side_effect = error
                response = self.client.get('/api/rubros/?format=tree',
                                           headers={'X-Request-Id': 'rubros-synthetic-demo'})
                self.assert_unavailable(response, 'rubros-synthetic-demo')

    def test_rollback_failure_preserves_safe_contract(self):
        self.query.all.side_effect = self.availability_errors()[0]
        self.rollback.side_effect = TimeoutError('PRIVATE_RUBROS_DIAGNOSTIC')
        self.assert_unavailable(self.client.get('/api/rubros/', headers={'X-Request-Id': 'rubros-rollback'}),
                                'rubros-rollback')

    def test_next_empty_request_recovers_without_demo_substitution(self):
        self.query.all.side_effect = [self.availability_errors()[0], []]
        self.assert_unavailable(self.client.get('/api/rubros/', headers={'X-Request-Id': 'rubros-recovery'}),
                                'rubros-recovery')
        response = self.client.get('/api/rubros/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])
        self.demo.assert_not_called()

    def test_legitimate_empty_queries_preserve_flat_and_tree_arrays(self):
        for base in self.BASES:
            for suffix in ('', '?format=tree'):
                with self.subTest(path=base + suffix):
                    response = self.client.get(base + suffix)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json(), [])
        self.demo.assert_not_called()

    def seed_rows(self):
        self.query.all.return_value = [
            SimpleNamespace(id=7, nombre='Instituciones', clave='instituciones',
                            descripcion='Synthetic public root', es_publico=True, padre_id=None),
            SimpleNamespace(id=8, nombre='Colegio', clave='colegio',
                            descripcion='Synthetic public child', es_publico=True, padre_id=7),
        ]

    def test_public_orm_rows_keep_legacy_and_api_flat_shape(self):
        self.seed_rows()
        for base in ('/rubros/', '/api/rubros/'):
            with self.subTest(path=base):
                response = self.client.get(base)
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual([row['id'] for row in body], [7, 8])
                self.assertEqual(body[1]['padre_id'], 7)
                self.assertTrue(body[1]['education_profile']['is_education'])
                self.assertIn('widget_preview', body[0])
                self.assertNotIn('demo', body[0])
        self.query.filter_by.assert_called_with(es_publico=True)

    def test_public_orm_rows_keep_legacy_and_api_tree_shape(self):
        self.seed_rows()
        for base in ('/rubros/', '/api/rubros/'):
            with self.subTest(path=base):
                response = self.client.get(base + '?format=tree')
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(len(body), 1)
                self.assertEqual(body[0]['id'], 7)
                self.assertEqual(body[0]['children'][0]['id'], 8)

    def assert_legacy_unexpected_error(self, error):
        self.query.all.side_effect = error
        for base in ('/rubros/', '/api/rubros/'):
            with self.subTest(path=base):
                response = self.client.get(base)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json(), {'error': 'Error interno al obtener los rubros.'})
                self.assertNotIn('PRIVATE_RUBROS', response.get_data(as_text=True))

    def test_integrity_error_is_not_reclassified_as_availability(self):
        self.assert_legacy_unexpected_error(IntegrityError('SYNTHETIC_SQL_SENTINEL', {},
                                                        Exception('PRIVATE_RUBROS_DIAGNOSTIC')))

    def test_database_looking_runtime_message_stays_legacy_500(self):
        self.assert_legacy_unexpected_error(RuntimeError('OperationalError quota PRIVATE_RUBROS_DIAGNOSTIC'))

    def test_post_stays_method_not_allowed_without_query(self):
        for base in ('/rubros/', '/api/rubros/'):
            with self.subTest(path=base):
                response = self.client.post(base)
                self.assertEqual(response.status_code, 405)
        self.query.all.assert_not_called()


if __name__ == '__main__':
    unittest.main()
