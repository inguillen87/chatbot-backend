"""Focused guard tests: real SQLite/SQLAlchemy, synthetic auth and lock adapter.

These tests do not instantiate create_app or exercise the real login decorators.
Full-app cases are separately defined in survey_workspace_http_acceptance.py.
"""
import ast
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import unittest

from sqlalchemy import Column, Integer, String, create_engine, event, select, text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Survey(Base):
    __tablename__ = 'enc_encuesta'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    estado = Column(String, nullable=True)
    structure_revision = Column(Integer, default=1)


class Response(Base):
    __tablename__ = 'enc_respuesta'
    id = Column(Integer, primary_key=True)
    encuesta_id = Column(Integer, nullable=False)
    tenant_id = Column(Integer, nullable=False)
    response_origin = Column(String)


class ServiceError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        self.status_code, self.payload = status_code, payload or {}
        super().__init__(message)


class DeletionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        self.sql = []
        event.listen(self.engine, 'before_cursor_execute',
                     lambda conn, cursor, stmt, params, ctx, many: self.sql.append(stmt))
        self.session.add(Survey(id=1, tenant_id=7, estado='borrador', structure_revision=1))
        self.session.commit()
        self.actor = SimpleNamespace(tenant_id=7)
        self.locks, self.authorizations = [], []
        self.before_lock = lambda: None
        self.commit_count = 0
        event.listen(self.session, 'after_commit', self.committed)

        def authorize(survey, actor):
            self.authorizations.append(survey.tenant_id)
            if survey.tenant_id != actor.tenant_id:
                raise ServiceError('denied', 403)

        def lock(identifier):
            self.before_lock()
            self.locks.append(identifier)
            # Adapter reproduces the shared guard's lock/refresh contract on SQLite;
            # it is not presented as a test of its production implementation.
            self.session.execute(text('UPDATE enc_encuesta SET structure_revision = structure_revision WHERE id = :id'), {'id': identifier})
            survey = self.session.get(Survey, identifier)
            self.session.refresh(survey)
            return survey

        dependencies = {}
        for name, fields in {
            'database': {'db': SimpleNamespace(session=self.session)},
            'models': {'EncEncuesta': Survey, 'EncRespuesta': Response},
            'services.encuestas_service': {'EncuestaError': ServiceError,
                '_ensure_tenant_access': authorize, '_acquire_encuesta_write_guard': lock},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(fields)
            dependencies[name] = module
        path = Path(__file__).resolve().parents[1] / 'services' / 'survey_deletion_policy.py'
        spec = importlib.util.spec_from_file_location('actual_deletion_policy', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, dependencies):
            spec.loader.exec_module(module)
        self.guard = module.require_admin_deletable_survey

    def committed(self, session): self.commit_count += 1

    def tearDown(self):
        self.session.rollback()
        self.session.close()
        self.engine.dispose()

    def add_response(self, origin='real', tenant=7):
        self.session.add(Response(encuesta_id=1, tenant_id=tenant, response_origin=origin))
        self.session.commit()
        self.commit_count = 0

    def change_state(self, state):
        self.session.get(Survey, 1).estado = state
        self.session.commit()
        self.commit_count = 0

    def assert_blocked(self, code):
        with self.assertRaises(ServiceError) as error:
            self.guard(1, self.actor)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.payload['reason_code'], code)
        self.assertFalse(error.exception.payload['retryable'])
        self.assertEqual(self.commit_count, 0)
        self.session.rollback()

    def test_empty_draft_is_allowed_without_commit_or_delete(self):
        self.guard(1, self.actor)
        self.assertEqual(self.locks, [1])
        self.assertEqual(self.authorizations, [7, 7])
        self.assertEqual(self.commit_count, 0)
        self.assertIsNotNone(self.session.get(Survey, 1))

    def test_published_is_blocked(self):
        self.change_state('publicada'); self.assert_blocked('survey_delete_requires_draft')

    def test_closed_is_blocked(self):
        self.change_state('cerrada'); self.assert_blocked('survey_delete_requires_draft')

    def test_archived_is_blocked(self):
        self.change_state('archivada'); self.assert_blocked('survey_delete_requires_draft')

    def test_missing_state_is_blocked(self):
        self.change_state(None); self.assert_blocked('survey_delete_requires_draft')

    def test_unknown_state_is_blocked(self):
        self.change_state('unknown'); self.assert_blocked('survey_delete_requires_draft')

    def test_real_response_is_preserved(self):
        self.add_response(); self.assert_blocked('survey_delete_has_responses')
        self.assertEqual(self.session.query(Response).count(), 1)

    def test_synthetic_response_is_preserved(self):
        self.add_response('synthetic_demo'); self.assert_blocked('survey_delete_has_responses')
        self.assertEqual(self.session.query(Response).count(), 1)

    def test_legacy_response_without_origin_is_preserved(self):
        self.add_response(None); self.assert_blocked('survey_delete_has_responses')

    def test_inconsistent_foreign_response_still_prevents_data_loss(self):
        self.add_response(tenant=8); self.assert_blocked('survey_delete_has_responses')

    def test_foreign_actor_cannot_lock_or_inspect_response_presence(self):
        with self.assertRaises(ServiceError) as error:
            self.guard(1, SimpleNamespace(tenant_id=8))
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.locks, [])
        self.assertFalse(any('enc_respuesta' in statement for statement in self.sql))

    def test_missing_id_fails_without_locking(self):
        with self.assertRaises(ServiceError) as error: self.guard(999, self.actor)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.locks, [])

    def test_post_lock_state_supersedes_the_earlier_draft(self):
        def changed():
            self.session.execute(text("UPDATE enc_encuesta SET estado='publicada' WHERE id=1"))
        self.before_lock = changed
        with self.assertRaises(ServiceError) as error: self.guard(1, self.actor)
        self.assertEqual(error.exception.payload['reason_code'], 'survey_delete_requires_draft')
        self.assertEqual(self.commit_count, 0)

    def test_ownership_is_rechecked_after_the_lock(self):
        self.before_lock = lambda: self.session.execute(text('UPDATE enc_encuesta SET tenant_id=8 WHERE id=1'))
        with self.assertRaises(ServiceError) as error: self.guard(1, self.actor)
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.authorizations, [7, 8])

    def test_response_existence_is_bounded_and_does_not_load_answer_payloads(self):
        self.add_response()
        self.sql.clear()
        with self.assertRaises(ServiceError): self.guard(1, self.actor)
        queries = [statement for statement in self.sql if 'enc_respuesta' in statement]
        self.assertEqual(len(queries), 1)
        self.assertIn('LIMIT', queries[0])
        self.assertNotIn('count(', queries[0].lower())

    def test_rejection_then_rollback_does_not_poison_next_valid_check(self):
        self.add_response()
        self.assert_blocked('survey_delete_has_responses')
        self.session.query(Response).delete()
        self.session.commit()
        self.commit_count = 0
        self.guard(1, self.actor)
        self.assertEqual(self.commit_count, 0)

    def test_shared_http_handler_calls_guard_before_existing_delete(self):
        source = Path(__file__).resolve().parents[1] / 'routes' / 'encuestas_admin.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_create_admin_blueprint')
        handler = next(node for node in factory.body if isinstance(node, ast.FunctionDef) and node.name == 'eliminar_encuesta_endpoint')
        transaction = next(node for node in handler.body if isinstance(node, ast.Try))
        calls = [node.value.func.id for node in transaction.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
        self.assertEqual(calls, ['require_admin_deletable_survey', 'delete_encuesta'])
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'rollback'
                            for node in ast.walk(transaction)))
        aliases = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Name) and node.func.id == '_create_admin_blueprint']
        self.assertEqual(len(aliases), 7)


if __name__ == '__main__':
    unittest.main(verbosity=2)
