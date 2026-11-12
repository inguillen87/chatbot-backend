"""Factory regressions for PostgreSQL owner locks and public failure envelopes."""
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import create_app, db
from config import TestConfig
from models import CategoriaTicket, TenantConfig, TenantProfile, User
from services.tenant_factory import create_tenant_from_template


class TestTenantFactoryOwnerLock(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner_lock_statements = []

        def observe_owner_lock(state):
            if state.is_select and state.bind_mapper is not None and state.bind_mapper.class_ is User:
                if state.statement._for_update_arg is not None:
                    self.owner_lock_statements.append(state.statement.compile(dialect=postgresql.dialect()))

        self.observe_owner_lock = observe_owner_lock
        event.listen(Session, 'do_orm_execute', observe_owner_lock)

    def tearDown(self):
        event.remove(Session, 'do_orm_execute', self.observe_owner_lock)
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def create(self, slug, **options):
        return create_tenant_from_template(nombre='Synthetic organization', slug=slug,
            tipo='pyme', plan='free', **options)

    def assert_postgres_lock_is_scalar_owner_query(self):
        self.assertEqual(len(self.owner_lock_statements), 1)
        compiled = self.owner_lock_statements[0]
        sql = str(compiled).upper()
        self.assertIn('FOR UPDATE', sql)
        self.assertIn('LOWER(', sql)
        self.assertIn('LIMIT', sql)
        self.assertNotIn('JOIN', sql, 'The owner lock must not include joined collection rows or nullable joins.')
        self.assertNotIn('CATEGORIA_TICKET', sql)
        self.assertIn(2, compiled.params.values(), 'Ambiguous email detection must retain the two-row bound.')

    def test_new_owner_factory_query_compiles_for_postgres_without_nullable_join(self):
        tenant = self.create('owner-lock-new')
        self.assertIsNotNone(tenant.id)
        self.assert_postgres_lock_is_scalar_owner_query()
        self.assertEqual(User.query.filter_by(email='admin@owner-lock-new.chatboc.local').count(), 1)
        self.assertTrue(TenantConfig.query.filter_by(tenant_id=tenant.id).count() >= 4)

    def test_existing_case_insensitive_owner_keeps_categories_password_and_lock(self):
        owner = User(email='Mixed-Owner@fixture.invalid', name='Existing fixture', rol='usuario')
        owner.set_password('synthetic-preserved-password')
        reference_owner = User(email='category-reference@fixture.invalid', name='Category reference', rol='admin', password_hash='synthetic-not-a-login-hash')
        db.session.add(reference_owner)
        db.session.flush()
        category_scope = TenantProfile(slug='category-only-fixture', nombre='Category-only fixture', tipo='pyme', pyme_id=reference_owner.id, is_active=True)
        category = CategoriaTicket(nombre='Synthetic category', tenant=category_scope)
        owner.categorias_ticket.append(category)
        db.session.add(owner)
        db.session.commit()
        owner_id, category_id, password_hash = owner.id, category.id, owner.password_hash
        db.session.expunge_all()
        tenant = self.create('owner-lock-existing', owner_email='mixed-owner@fixture.invalid', allow_existing_owner=True)
        self.assert_postgres_lock_is_scalar_owner_query()
        preserved = db.session.get(User, owner_id)
        self.assertEqual(tenant.pyme_id, owner_id)
        self.assertEqual(preserved.password_hash, password_hash)
        self.assertEqual([item.id for item in preserved.categorias_ticket], [category_id])

    def test_bound_owner_rejection_does_not_mutate_existing_identity_or_create_tenant(self):
        owner = User(email='bound-owner@fixture.invalid', name='Bound fixture', rol='admin', tenant_slug='original-scope')
        owner.set_password('synthetic-preserved-password')
        db.session.add(owner)
        db.session.commit()
        owner_id, password_hash = owner.id, owner.password_hash
        before = (TenantProfile.query.count(), TenantConfig.query.count(), User.query.count())
        with self.assertRaisesRegex(ValueError, 'already linked'):
            self.create('owner-lock-foreign', owner_email=owner.email, allow_existing_owner=True)
        preserved = db.session.get(User, owner_id)
        self.assertEqual(preserved.tenant_slug, 'original-scope')
        self.assertEqual(preserved.password_hash, password_hash)
        self.assertEqual((TenantProfile.query.count(), TenantConfig.query.count(), User.query.count()), before)
        self.assert_postgres_lock_is_scalar_owner_query()

    def test_database_failure_response_does_not_disclose_sql_parameters_or_internal_email(self):
        sql = 'SELECT private_owner_secret FROM user FOR UPDATE'
        params = {'email': 'admin@synthetic-private.chatboc.local', 'password': 'synthetic-private-password'}
        failure = OperationalError(sql, params, RuntimeError('nullable outer join private diagnostic'))
        with patch('routes.admin_tenant.create_tenant_from_template', side_effect=failure):
            response = self.client.post('/api/admin/tenants', json={'nombre': 'Fixture', 'slug': 'owner-lock-error', 'tipo': 'pyme'})
        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body.get('reason_code'), 'tenant_creation_failed')
        self.assertEqual(body.get('error'), 'No se pudo confirmar la creación de la organización. Consultá el estado antes de volver a intentarlo.')
        serialized = response.get_data(as_text=True)
        for private in [sql, 'SELECT', 'private_owner_secret', params['email'], params['password'], 'outer join', 'OperationalError']:
            self.assertNotIn(private, serialized)
        self.assertIsNone(TenantProfile.query.filter_by(slug='owner-lock-error').first())

    def test_post_commit_response_failure_preserves_created_organization_without_claiming_absence(self):
        with patch('routes.admin_tenant.build_tenant_provisioning_readiness', side_effect=RuntimeError('synthetic post-commit private diagnostic')):
            response = self.client.post('/api/admin/tenants', json={'nombre': 'Fixture', 'slug': 'owner-lock-uncertain', 'tipo': 'pyme'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {
            'error': 'No se pudo confirmar la creación de la organización. Consultá el estado antes de volver a intentarlo.',
            'reason_code': 'tenant_creation_failed',
        })
        self.assertEqual(TenantProfile.query.filter_by(slug='owner-lock-uncertain').count(), 1)
        self.assertEqual(User.query.filter_by(email='admin@owner-lock-uncertain.chatboc.local').count(), 1)
        self.assertNotIn('synthetic post-commit private diagnostic', response.get_data(as_text=True))

    def test_expected_validation_failure_keeps_existing_400_contract(self):
        with patch('routes.admin_tenant.create_tenant_from_template', side_effect=ValueError('slug is required')):
            response = self.client.post('/api/admin/tenants', json={'nombre': 'Fixture', 'tipo': 'pyme'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'slug is required'})


if __name__ == '__main__':
    unittest.main()
