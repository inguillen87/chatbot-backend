"""Isolated ORM transactions and real native-login/HTTP denial checks.

Success authorization of a live Clerk SuperAdmin remains a Preview check. No
provider verification is mocked or remote credential/token fabricated here.
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

import copy
import os
import tempfile
import unittest
from uuid import uuid4


class NativeAdminMembershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        from models import User, TenantProfile
        from database import db
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-native-admin-membership-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        cls.prior_allowlist = os.environ.get('CLERK_SUPERADMIN_EMAILS')
        os.environ['CLERK_SUPERADMIN_EMAILS'] = 'platform-acceptance@example.invalid'
        with cls.app.app_context():
            actor = User(name='Platform acceptance', email='platform-acceptance@example.invalid',
                rol='super_admin', accesibilidad={'auth': {'provider': 'clerk', 'clerk': {'user_id': 'acceptance-platform'}}})
            actor.set_password(cls.password)
            db.session.add(actor)
            target = db.session.get(User, cls.accounts['second']['id'])
            target.rol = 'admin_municipio'
            target.municipio_id = target.id
            db.session.commit()
            cls.actor_id = actor.id
            cls.user_baselines = {user.id: {key: copy.deepcopy(getattr(user, key)) for key in (
                'rol', 'tipo_chat', 'tenant_id', 'tenant_slug', 'municipio_id', 'pyme_id', 'empresa_id', 'accesibilidad')}
                for user in User.query.all()}
            cls.tenant_baselines = {tenant.id: {key: getattr(tenant, key) for key in (
                'slug', 'tipo', 'municipio_id', 'pyme_id', 'is_active')} for tenant in TenantProfile.query.all()}

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.temp.cleanup()
        if cls.prior_allowlist is None:
            os.environ.pop('CLERK_SUPERADMIN_EMAILS', None)
        else:
            os.environ['CLERK_SUPERADMIN_EMAILS'] = cls.prior_allowlist

    def setUp(self):
        from database import db
        from models import AdminAuditLog, TenantProfile, User
        self.context = self.app.app_context()
        self.context.push()
        db.session.rollback()
        for user_id, values in self.user_baselines.items():
            user = db.session.get(User, user_id)
            for key, value in values.items():
                setattr(user, key, copy.deepcopy(value))
        for tenant_id, values in self.tenant_baselines.items():
            tenant = db.session.get(TenantProfile, tenant_id)
            for key, value in values.items():
                setattr(tenant, key, value)
        AdminAuditLog.query.delete()
        db.session.commit()
        self.actor = db.session.get(User, self.actor_id)
        self.target = db.session.get(User, self.accounts['second']['id'])
        self.tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
        self.owner = db.session.get(User, self.accounts['acceptance-a']['id'])

    def tearDown(self):
        from database import db
        db.session.rollback()
        db.session.remove()
        self.context.pop()

    def read(self):
        from database import db
        from services.native_admin_membership import read_native_admin_membership
        return read_native_admin_membership(db.session, actor=self.actor,
            slug=self.tenant.slug, user_id=self.target.id)

    def apply(self, descriptor=None, request_id=None, **extras):
        from database import db
        from utils.auth_helpers import auth_session_version
        from services.native_admin_membership import normalize_native_admin_membership
        descriptor = descriptor or self.read()
        data = {'expected_revision': descriptor['expected_revision'], 'request_id': request_id or str(uuid4()), **extras}
        return normalize_native_admin_membership(db.session, actor=self.actor,
            actor_session_version=auth_session_version(self.actor),
            slug=self.tenant.slug, user_id=self.target.id, data=data)

    def test_listing_uses_real_explicit_ids_and_excludes_owner_and_other_tenant(self):
        from database import db
        from services.native_admin_membership import list_native_admin_memberships
        result = list_native_admin_memberships(db.session, actor=self.actor, slug=self.tenant.slug)
        self.assertEqual(result['contract_version'], 'native_admin.legacy_membership_list.v1')
        self.assertEqual([item['target']['id'] for item in result['items']], [self.target.id])
        self.assertTrue(result['items'][0]['can_apply'])
        self.assertFalse(result['permissions']['credentials_change_allowed'])

    def test_normalization_changes_only_legacy_reference_and_audits_atomically(self):
        from database import db
        from models import AdminAuditLog
        before = self.read()
        unchanged = {key: getattr(self.target, key) for key in ('password_hash', 'rol', 'tenant_id', 'tenant_slug', 'empresa_id', 'pyme_id')}
        owner_hash = self.owner.password_hash
        result = self.apply(before)
        self.assertFalse(result['can_apply'])
        self.assertEqual(result['state'], 'already_consistent')
        self.assertEqual(result['action_receipt']['revision'], result['expected_revision'])
        self.assertEqual(result['action_receipt']['old_reference'], self.target.id)
        self.assertEqual(result['action_receipt']['new_reference'], self.owner.id)
        self.assertTrue(all(getattr(self.target, key) == value for key, value in unchanged.items()))
        self.assertTrue(self.owner.password_hash == owner_hash)
        self.assertEqual(self.tenant.municipio_id, self.owner.id)
        self.assertEqual(AdminAuditLog.query.count(), 1)
        details = AdminAuditLog.query.one().details
        self.assertEqual(details['changed_fields'], ['municipio_id'])
        self.assertFalse(details['credentials_changed'])
        self.assertNotIn('password', str(details))

    def test_same_request_and_revision_returns_confirmed_receipt_without_second_audit(self):
        from models import AdminAuditLog
        before = self.read()
        request_id = str(uuid4())
        first = self.apply(before, request_id)
        second = self.apply(before, request_id)
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(second['action_receipt'], first['action_receipt'])
        self.assertEqual(AdminAuditLog.query.count(), 1)

    def test_revoked_superadmin_session_cannot_mutate_or_confirm_a_receipt(self):
        from database import db
        from utils.auth_helpers import auth_session_version, bump_auth_session_version
        from services.native_admin_membership import NativeAdminMembershipError, normalize_native_admin_membership
        before = self.read()
        old_session_version = auth_session_version(self.actor)
        bump_auth_session_version(self.actor)
        db.session.commit()
        with self.assertRaises(NativeAdminMembershipError) as revoked:
            normalize_native_admin_membership(db.session, actor=self.actor,
                actor_session_version=old_session_version, slug=self.tenant.slug, user_id=self.target.id,
                data={'expected_revision': before['expected_revision'], 'request_id': str(uuid4())})
        self.assertEqual(revoked.exception.status, 403)
        self.assertEqual(self.target.municipio_id, self.target.id)

    def test_stale_revision_and_reused_request_for_changed_input_fail_closed(self):
        from services.native_admin_membership import NativeAdminMembershipError
        before = self.read()
        request_id = str(uuid4())
        after = self.apply(before, request_id)
        for descriptor, nonce in ((before, str(uuid4())), (after, request_id)):
            with self.subTest(revision=descriptor['expected_revision'] == before['expected_revision']):
                with self.assertRaises(NativeAdminMembershipError) as rejected:
                    self.apply(descriptor, nonce)
                self.assertEqual(rejected.exception.status, 412)

    def test_foreign_or_contradictory_explicit_scope_is_not_a_target(self):
        from database import db
        from services.native_admin_membership import NativeAdminMembershipError, read_native_admin_membership
        with self.assertRaises(NativeAdminMembershipError) as foreign:
            read_native_admin_membership(db.session, actor=self.actor,
                slug='acceptance-b', user_id=self.target.id)
        self.assertEqual(foreign.exception.status, 404)
        self.target.tenant_slug = 'acceptance-b'
        db.session.commit()
        with self.assertRaises(NativeAdminMembershipError) as conflict:
            self.read()
        self.assertEqual(conflict.exception.status, 404)

    def test_owner_cannot_be_normalized_as_a_nonowner_admin(self):
        from database import db
        from services.native_admin_membership import read_native_admin_membership
        result = read_native_admin_membership(db.session, actor=self.actor,
            slug=self.tenant.slug, user_id=self.owner.id)
        self.assertFalse(result['can_apply'])
        self.assertEqual(result['reason_code'], 'target_is_tenant_owner')

    def test_other_legacy_references_and_clerk_disabled_or_inactive_are_blocked(self):
        from database import db
        from services.native_admin_membership import NativeAdminMembershipError
        cases = [
            ('municipio_id', self.accounts['acceptance-b']['id'], 'legacy_self_reference_required'),
            ('empresa_id', self.owner.id, 'conflicting_legacy_reference'),
            ('pyme_id', self.owner.id, 'conflicting_legacy_reference'),
            ('accesibilidad', {'auth': {'provider': 'clerk'}}, 'native_admin_required'),
            ('accesibilidad', {'auth': {'provider': 'unverified-oauth'}}, 'native_admin_required'),
            ('accesibilidad', {'auth': {'disabled': True}}, 'native_admin_unavailable'),
            ('rol', 'empleado', 'native_municipality_admin_required'),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field, reason=reason):
                original = copy.deepcopy(getattr(self.target, field))
                setattr(self.target, field, value)
                db.session.commit()
                descriptor = self.read()
                self.assertFalse(descriptor['can_apply'])
                self.assertEqual(descriptor['reason_code'], reason)
                with self.assertRaises(NativeAdminMembershipError) as blocked:
                    self.apply(descriptor)
                self.assertEqual(blocked.exception.status, 409)
                setattr(self.target, field, original)
                db.session.commit()
        self.tenant.is_active = False
        db.session.commit()
        self.assertEqual(self.read()['reason_code'], 'tenant_inactive')

    def test_target_foreign_ownership_and_shared_organization_owner_are_blocked(self):
        from database import db
        from models import TenantProfile
        other = db.session.get(TenantProfile, self.accounts['acceptance-b']['tenant_id'])
        original = other.municipio_id
        for new_owner, reason in ((self.target.id, 'target_is_tenant_owner'),
                                  (self.owner.id, 'organization_owner_ambiguous')):
            with self.subTest(reason=reason):
                other.municipio_id = new_owner
                db.session.commit()
                self.assertFalse(self.read()['can_apply'])
                self.assertEqual(self.read()['reason_code'], reason)
                other.municipio_id = original
                db.session.commit()

    def test_handler_contract_rejects_conflicting_or_duplicate_tenant_selectors(self):
        # Execute only the real handler contract here. The HTTP auth decorators
        # are exercised separately with anonymous and native-login principals.
        from routes.super_admin import native_admin_legacy_membership_list
        handler = native_admin_legacy_membership_list.__wrapped__.__wrapped__
        path = f'/api/admin/tenants/{self.tenant.slug}/native-admin-users/legacy-membership'
        with self.app.test_request_context(path, query_string={'tenant': self.tenant.slug, 'tenant_slug': self.tenant.slug},
                headers={'X-Tenant-Slug': self.tenant.slug}):
            response = handler(self.actor, self.tenant.slug)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['items'][0]['target']['id'], self.target.id)
            self.assertIn('no-store', response.headers['Cache-Control'])
        for query, headers in (({'tenant': 'acceptance-b'}, {}),
                ([('tenant', self.tenant.slug), ('tenant', 'acceptance-b')], {}),
                ({}, {'X-Tenant-Id': str(self.accounts['acceptance-b']['tenant_id'])})):
            with self.subTest(query=query, headers=headers), self.app.test_request_context(path, query_string=query, headers=headers):
                response = handler(self.actor, self.tenant.slug)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()['reason_code'], 'tenant_selector_conflict')

    def test_owner_reference_changed_since_review_is_a_revision_conflict(self):
        from database import db
        from services.native_admin_membership import NativeAdminMembershipError
        before = self.read()
        self.tenant.municipio_id = self.accounts['acceptance-b']['id']
        db.session.commit()
        with self.assertRaises(NativeAdminMembershipError) as conflict:
            self.apply(before)
        self.assertEqual(conflict.exception.status, 412)
        self.assertEqual(self.target.municipio_id, self.target.id)

    def test_password_or_role_payload_is_rejected_without_writes(self):
        from models import AdminAuditLog
        from services.native_admin_membership import NativeAdminMembershipError
        for field in ('password', 'rol', 'tenant_id', 'municipio_id'):
            with self.subTest(field=field), self.assertRaises(NativeAdminMembershipError) as invalid:
                self.apply(**{field: 'not-accepted'})
            self.assertEqual(invalid.exception.status, 400)
        self.assertEqual(AdminAuditLog.query.count(), 0)
        self.assertEqual(self.target.municipio_id, self.target.id)

    def test_audit_insert_failure_rolls_back_the_reference_change(self):
        from database import db
        from models import AdminAuditLog
        from services.native_admin_membership import NativeAdminMembershipError
        db.session.connection().exec_driver_sql("CREATE TRIGGER reject_membership_audit BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(ABORT, 'acceptance audit unavailable'); END")
        db.session.commit()
        try:
            with self.assertRaises(NativeAdminMembershipError) as failure:
                self.apply()
            self.assertEqual(failure.exception.status, 503)
            self.assertEqual(self.target.municipio_id, self.target.id)
            self.assertEqual(AdminAuditLog.query.count(), 0)
        finally:
            db.session.connection().exec_driver_sql('DROP TRIGGER reject_membership_audit')
            db.session.commit()

    def test_anonymous_and_real_native_admin_http_cannot_use_superadmin_resource(self):
        path = f'/api/admin/tenants/{self.tenant.slug}/native-admin-users/legacy-membership'
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(path).status_code, 401)
        client = self.app.test_client()
        self.assertEqual(client.post('/auth/login', json={
            'email': self.accounts['acceptance-a']['email'], 'password': self.password}).status_code, 200)
        self.assertEqual(client.get(path).status_code, 403)
        individual = f'/api/admin/tenants/{self.tenant.slug}/native-admin-users/{self.target.id}/legacy-membership'
        self.assertEqual(client.put(individual, json={'expected_revision': self.read()['expected_revision'], 'request_id': str(uuid4())}).status_code, 403)

    def test_unchanged_password_logs_into_normal_panel_after_reference_is_consistent(self):
        client = self.app.test_client()
        payload = {'email': self.accounts['second']['email'], 'password': self.password}
        before = client.post('/api/auth/admin/login', json=payload)
        self.assertEqual(before.status_code, 200)
        before_headers = {'Authorization': 'Bearer ' + before.get_json()['token']}
        before_profile = client.get('/api/me', headers=before_headers)
        self.assertEqual(before_profile.status_code, 200)
        self.assertNotIn('knowledge.read', before_profile.get_json()['capabilities'])
        self.assertEqual(client.get('/api/v2/tenants/acceptance-a/integrations/whatsapp/status', headers=before_headers).status_code, 403)
        self.apply()
        after = client.post('/api/auth/admin/login', json=payload)
        self.assertEqual(after.status_code, 200)
        # This JWT is issued by the real local password endpoint, never fabricated.
        headers = {'Authorization': 'Bearer ' + after.get_json()['token']}
        profile = client.get('/api/me', headers=headers)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.get_json()['tenant_slug'], self.tenant.slug)
        self.assertIn('knowledge.read', profile.get_json()['capabilities'])
        self.assertEqual(client.get('/api/v2/tenants/acceptance-a/integrations/whatsapp/status', headers=headers).status_code, 200)
        self.assertEqual(client.get('/api/v2/tenants/acceptance-b/integrations/whatsapp/status', headers=headers).status_code, 403)


if __name__ == '__main__':
    unittest.main()
