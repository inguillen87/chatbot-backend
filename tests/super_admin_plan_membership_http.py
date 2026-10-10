"""Plan propagation on the actual ORM/handler and HTTP denial boundaries.

Uses an isolated full application with outbound networks disabled. Positive
cases retain the actual SuperAdmin role/allowlist decorator while invoking the
handler below token authentication; they do not certify a live Clerk session.
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

import os
import tempfile
import unittest


class SuperAdminPlanMembershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-plan-membership-')
        os.environ['CORS_ALLOWED_ORIGINS'] = 'https://panel.example.invalid'
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        cls.previous_allowlist = os.environ.get('CLERK_SUPERADMIN_EMAILS')
        os.environ['CLERK_SUPERADMIN_EMAILS'] = 'plan-platform@example.invalid'

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.temp.cleanup()
        if cls.previous_allowlist is None:
            os.environ.pop('CLERK_SUPERADMIN_EMAILS', None)
        else:
            os.environ['CLERK_SUPERADMIN_EMAILS'] = cls.previous_allowlist

    def setUp(self):
        from database import db
        from models import AdminAuditLog, TenantProfile, User
        self.context = self.app.app_context()
        self.context.push()
        db.session.rollback()
        AdminAuditLog.query.delete()
        TenantProfile.query.filter(TenantProfile.id >= 9000).delete()
        User.query.filter(User.id >= 9000).delete()
        db.session.commit()

    def tearDown(self):
        from database import db
        db.session.rollback()
        db.session.remove()
        self.context.pop()

    def seed_graph(self, kind):
        from database import db
        from models import TenantProfile, User
        field = 'municipio_id' if kind == 'municipio' else 'pyme_id'
        self.owner = User(id=9101, name='Target owner', email='plan-owner@example.invalid',
                          rol='admin', tipo_chat=kind)
        self.foreign_owner = User(id=9001, name='Foreign owner', email='plan-foreign-owner@example.invalid',
                                  rol='admin', tipo_chat=kind)
        self.actor = User(id=9900, name='Platform fixture', email='plan-platform@example.invalid',
                          rol='super_admin', tipo_chat='admin')
        for user in (self.owner, self.foreign_owner, self.actor):
            user.set_password(self.password)
        db.session.add_all([self.owner, self.foreign_owner, self.actor])
        db.session.flush()
        # The target TENANT id deliberately equals another organization's USER
        # owner id. Those two identifier domains must never be interchangeable.
        self.tenant = TenantProfile(id=9001, slug='plan-target', nombre='Target', tipo=kind,
                                    plan='pro', **{field: self.owner.id})
        self.foreign = TenantProfile(id=9002, slug='plan-foreign', nombre='Foreign', tipo=kind,
                                     plan='pro', **{field: self.foreign_owner.id})
        db.session.add_all([self.tenant, self.foreign])
        db.session.flush()
        setattr(self.owner, field, self.owner.id)
        # Target owner deliberately has no direct tenant_id/tenant_slug.
        self.foreign_owner.tenant_id = self.foreign.id
        self.foreign_owner.tenant_slug = self.foreign.slug
        setattr(self.foreign_owner, field, self.foreign_owner.id)
        specs = {
            'direct': dict(tenant_id=self.tenant.id, tenant_slug=self.tenant.slug),
            'legacy': {field: self.owner.id},
            'employee': dict(empresa_id=self.owner.id),
            'foreign': dict(tenant_id=self.foreign.id, tenant_slug=self.foreign.slug,
                            **{field: self.foreign_owner.id}),
            'conflicting_owner': dict(tenant_id=self.tenant.id, tenant_slug=self.tenant.slug,
                                      **{field: self.foreign_owner.id}),
            'conflicting_slug': dict(tenant_id=self.tenant.id, tenant_slug=self.foreign.slug),
            'conflicting_employer': dict(tenant_id=self.foreign.id, tenant_slug=self.foreign.slug,
                                         empresa_id=self.owner.id),
        }
        self.members = {}
        for index, (label, attrs) in enumerate(specs.items(), start=9201):
            member = User(id=index, name=label, email=f'plan-{label}@example.invalid',
                          rol='empleado', tipo_chat=kind, **attrs)
            member.set_password(self.password)
            db.session.add(member)
            self.members[label] = member
        for user in (self.owner, self.foreign_owner, self.actor, *self.members.values()):
            user.plan = 'pro'
            user.limite_preguntas = 250
            user.preapproval_id = 'fixture-preapproval'
            user.plan_status = 'fixture-active'
        db.session.commit()

    def apply(self, *, actor=None, **extras):
        from routes.super_admin import update_tenant_full
        # Only token_requerido is unwrapped. The original SuperAdmin decorator
        # still enforces real role/allowlist authorization on this local actor.
        handler = update_tenant_full.__wrapped__
        with self.app.test_request_context(f'/api/admin/tenants/{self.tenant.slug}', method='PUT',
                                           json={'plan': 'full', **extras}):
            result = handler(actor or self.actor, self.tenant.slug)
        response, status = result if isinstance(result, tuple) else (result, result.status_code)
        return response.get_json(), status

    def assert_exact_propagation(self):
        from database import db
        from models import AdminAuditLog, TenantProfile, User
        payload, status = self.apply()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload['plan_applied'], 'full')
        db.session.expire_all()
        self.assertEqual(db.session.get(TenantProfile, self.tenant.id).plan, 'full')
        self.assertEqual(db.session.get(TenantProfile, self.foreign.id).plan, 'pro')
        admitted = [self.owner, *(self.members[key] for key in ('direct', 'legacy', 'employee'))]
        rejected = [self.foreign_owner, self.actor, *(self.members[key] for key in (
            'foreign', 'conflicting_owner', 'conflicting_slug', 'conflicting_employer'))]
        for user in admitted:
            with self.subTest(admitted=user.id):
                fresh = db.session.get(User, user.id)
                self.assertEqual(fresh.plan, 'full')
                self.assertIsNone(fresh.limite_preguntas)
                self.assertIsNone(fresh.preapproval_id)
                self.assertIsNone(fresh.plan_status)
        for user in rejected:
            with self.subTest(excluded=user.id):
                fresh = db.session.get(User, user.id)
                self.assertEqual((fresh.plan, fresh.limite_preguntas, fresh.preapproval_id, fresh.plan_status),
                                 ('pro', 250, 'fixture-preapproval', 'fixture-active'))
        audit = AdminAuditLog.query.filter_by(action='change_plan', target_object='plan-target').one()
        self.assertEqual(audit.admin_user_id, self.actor.id)
        self.assertEqual(audit.details, {'old': 'pro', 'new': 'full'})

    def test_municipality_collision_excludes_foreign_and_conflicting_members(self):
        self.seed_graph('municipio')
        self.assert_exact_propagation()

    def test_business_collision_excludes_foreign_and_conflicting_members(self):
        self.seed_graph('pyme')
        self.assert_exact_propagation()

    def test_owner_without_any_direct_or_legacy_membership_keeps_fk_scope(self):
        from database import db
        from models import User
        for kind in ('municipio', 'pyme'):
            with self.subTest(kind=kind):
                self.seed_graph(kind)
                self.owner.municipio_id = self.owner.pyme_id = self.owner.empresa_id = None
                self.owner.tenant_id = self.owner.tenant_slug = None
                db.session.commit()
                payload, status = self.apply()
                self.assertEqual(status, 200, payload)
                db.session.expire_all()
                self.assertEqual(db.session.get(User, self.owner.id).plan, 'full')
                if kind == 'municipio':
                    # Reset this disposable graph before the other subcase.
                    self.setUp_graph_cleanup()

    def setUp_graph_cleanup(self):
        from database import db
        from models import AdminAuditLog, TenantProfile, User
        AdminAuditLog.query.delete()
        TenantProfile.query.filter(TenantProfile.id >= 9000).delete()
        User.query.filter(User.id >= 9000).delete()
        db.session.commit()

    def test_shared_owner_does_not_propagate_ambiguous_legacy_membership(self):
        from database import db
        from models import User
        self.seed_graph('municipio')
        self.foreign.municipio_id = self.owner.id
        db.session.commit()
        payload, status = self.apply()
        self.assertEqual(status, 200, payload)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.members['direct'].id).plan, 'full')
        self.assertEqual(db.session.get(User, self.foreign_owner.id).plan, 'pro')
        self.assertEqual(db.session.get(User, self.owner.id).plan, 'pro')

    def test_conflicting_owner_is_not_propagated(self):
        from database import db
        from models import User
        self.seed_graph('municipio')
        self.owner.tenant_id = self.foreign.id
        self.owner.tenant_slug = self.foreign.slug
        db.session.commit()
        payload, status = self.apply()
        self.assertEqual(status, 200, payload)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.owner.id).plan, 'pro')
        self.assertEqual(db.session.get(User, self.members['employee'].id).plan, 'pro')
        self.assertEqual(db.session.get(User, self.members['direct'].id).plan, 'full')

    def test_combined_slug_and_plan_change_uses_refreshed_direct_membership(self):
        from database import db
        from models import User
        self.seed_graph('municipio')
        # Keep members loaded: the route's existing bulk rename uses
        # synchronize_session=False, so propagation must read fresh membership.
        member = self.members['direct']
        self.assertEqual(member.tenant_slug, 'plan-target')
        payload, status = self.apply(slug='plan-renamed')
        self.assertEqual(status, 200, payload)
        db.session.expire_all()
        fresh = db.session.get(User, member.id)
        self.assertEqual((fresh.tenant_slug, fresh.plan), ('plan-renamed', 'full'))
        self.assertEqual(db.session.get(User, self.foreign_owner.id).plan, 'pro')

    def test_existing_superadmin_role_allowlist_and_http_denials_remain(self):
        from database import db
        from models import AdminAuditLog, User
        self.seed_graph('municipio')
        self.actor.rol = 'admin'
        payload, status = self.apply()
        self.assertEqual(status, 403, payload)
        self.actor.rol = 'super_admin'
        self.actor.email = 'plan-not-allowed@example.invalid'
        payload, status = self.apply()
        self.assertEqual(status, 403, payload)
        db.session.rollback()
        self.assertEqual(AdminAuditLog.query.count(), 0)
        path = '/api/admin/tenants/plan-target'
        anonymous = self.app.test_client().put(path, json={'plan': 'full'})
        self.assertIn(anonymous.status_code, (401, 403))
        native = self.app.test_client()
        login = native.post('/auth/login', json={
            'email': self.accounts['acceptance-a']['email'], 'password': self.password})
        self.assertEqual(login.status_code, 200, login.get_json())
        denied = native.put(path, json={'plan': 'full'})
        self.assertEqual(denied.status_code, 403, denied.get_json())
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.foreign_owner.id).plan, 'pro')
        self.assertEqual(AdminAuditLog.query.count(), 0)

    def test_legacy_name_change_rejects_every_combined_write_without_audit(self):
        from database import db
        from models import AdminAuditLog, TenantProfile, User
        self.seed_graph('municipio')
        self.owner.nombre_empresa = 'Owner institutional name'
        db.session.commit()
        before_users = {user.id: (user.rol, user.plan, user.tenant_id, user.tenant_slug,
                                 user.nombre_empresa, user.accesibilidad)
                        for user in User.query.all()}
        for requested in ('New name', 'Target ', '', None, True, {'name': 'Target'}):
            with self.subTest(nombre=requested):
                payload, status = self.apply(nombre=requested, is_active=False,
                                             slug='plan-renamed', dominio='new.example.invalid')
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload['reason_code'], 'organization_name_requires_profile_update')
                self.assertEqual(payload['save_endpoint'], '/api/admin/tenants/plan-target/config')
                # Prove no pending mutation can be committed later either.
                db.session.commit()
                db.session.expire_all()
                tenant = db.session.get(TenantProfile, self.tenant.id)
                self.assertEqual((tenant.nombre, tenant.slug, tenant.plan, tenant.is_active,
                                  tenant.dominio), ('Target', 'plan-target', 'pro', True, None))
                self.assertEqual({user.id: (user.rol, user.plan, user.tenant_id, user.tenant_slug,
                                          user.nombre_empresa, user.accesibilidad)
                                 for user in User.query.all()}, before_users)
                self.assertEqual(AdminAuditLog.query.count(), 0)

    def test_equal_legacy_name_is_not_written_and_plan_remains_compatible(self):
        from database import db
        from models import AdminAuditLog, TenantProfile, User
        from sqlalchemy import event
        self.seed_graph('pyme')
        self.owner.nombre_empresa = 'Distinct existing owner name'
        db.session.commit()
        writes = []
        def capture(target, value, oldvalue, initiator):
            writes.append(value)
        event.listen(TenantProfile.nombre, 'set', capture)
        try:
            payload, status = self.apply(nombre='Target', is_active=True)
        finally:
            event.remove(TenantProfile.nombre, 'set', capture)
        self.assertEqual(status, 200, payload)
        self.assertEqual(writes, [])
        db.session.expire_all()
        self.assertEqual(db.session.get(TenantProfile, self.tenant.id).nombre, 'Target')
        self.assertEqual(db.session.get(User, self.owner.id).nombre_empresa, 'Distinct existing owner name')
        self.assertEqual(db.session.get(TenantProfile, self.tenant.id).plan, 'full')
        self.assertEqual(AdminAuditLog.query.filter_by(action='change_plan').count(), 1)

    def test_name_validation_does_not_disclose_before_auth_or_for_missing_tenant(self):
        from database import db
        from models import AdminAuditLog
        from routes.super_admin import update_tenant_full
        from werkzeug.exceptions import NotFound
        self.seed_graph('municipio')
        self.actor.rol = 'admin'
        payload, status = self.apply(nombre='New name', is_active=False)
        self.assertEqual(status, 403, payload)
        self.assertNotIn('organization_name_requires_profile_update', str(payload))
        self.actor.rol = 'super_admin'
        with self.app.test_request_context('/api/admin/tenants/absent', method='PUT',
                                           json={'nombre': 'New name', 'plan': 'full'}):
            with self.assertRaises(NotFound):
                update_tenant_full.__wrapped__(self.actor, 'absent')
        db.session.rollback()
        anonymous = self.app.test_client().put('/api/admin/tenants/plan-target',
                                              json={'nombre': 'New name', 'plan': 'full'})
        self.assertIn(anonymous.status_code, (401, 403))
        self.assertNotIn('organization_name_requires_profile_update', str(anonymous.get_json()))
        self.assertEqual(AdminAuditLog.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
