"""Actual mapped eager joins; run in isolated full-runtime CI, without SQL I/O.

The minimal transaction job deliberately does not install full model imports.
This regression retains the actual User relationship and service SQL queries.
"""
from types import SimpleNamespace
import unittest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session as OrmSession
from models import User, TenantProfile, AuditEvent
from services.organization_profile_settings import save_profile_settings


class RealProfileLockTests(unittest.TestCase):
    def test_real_user_eager_categories_lock_only_sorted_actor_and_owner(self):
        statements, rollbacks = {}, []
        class CapturedLocks(Exception): pass
        class CaptureQuery(Query):
            def one_or_none(query):
                statements['tenant'] = query.statement.compile(
                    dialect=postgresql.dialect(), compile_kwargs={'literal_binds':True})
                return SimpleNamespace(id=46, is_active=True, municipio_id=9, pyme_id=None)
            def all(query):
                statements['users'] = query.statement.compile(dialect=postgresql.dialect())
                raise CapturedLocks()
        orm_session = OrmSession(query_cls=CaptureQuery)
        def setup_lock_timeout(statement):
            self.assertEqual(str(statement), "SET LOCAL lock_timeout = '5s'")
        session = SimpleNamespace(query=orm_session.query,
            get_bind=lambda:SimpleNamespace(dialect=postgresql.dialect()),
            execute=setup_lock_timeout, rollback=lambda:rollbacks.append(True))
        try:
            with self.assertRaises(CapturedLocks):
                save_profile_settings(session,TenantProfile,User,AuditEvent,tenant_id=46,actor_id=5,
                    data={'organization_profile':{'logo_url':'https://example.test/logo.png'},
                          'expected_revision':'0'*64},authorize=lambda actor,tenant:True)
        finally:
            orm_session.close()
        tenant_sql, user_sql = str(statements['tenant']), str(statements['users'])
        self.assertTrue(tenant_sql.endswith('FOR UPDATE OF tenant_profile'))
        self.assertIn('LEFT OUTER JOIN', user_sql)
        self.assertEqual(User.categorias_ticket.property.lazy, 'joined')
        self.assertIn('ORDER BY "user".id', user_sql)
        self.assertTrue(user_sql.endswith('FOR UPDATE OF "user"'))
        self.assertEqual(len(statements['users'].params), 1)
        self.assertEqual(set(next(iter(statements['users'].params.values()))), {5,9})
        self.assertEqual(rollbacks, [True])
