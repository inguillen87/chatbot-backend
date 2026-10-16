"""Actual V2 membership, normal login and ACL on an isolated full application."""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

import tempfile
import unittest
from unittest.mock import patch


class V2TenantMembershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-v2-membership-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.temp.cleanup()

    def membership(self, label, change=None):
        from database import db
        from models import TenantProfile, User
        from routes.v2.saas import _user_can_access_tenant
        with self.app.app_context():
            user = db.session.get(User, self.accounts[label]['id'])
            if change:
                change(user)
            result = {
                slug: _user_can_access_tenant(user, db.session.get(TenantProfile, self.accounts[slug]['tenant_id']))
                for slug in ('acceptance-a', 'acceptance-b')
            }
            db.session.rollback()
            return result

    def assert_denied_both(self, label, change):
        self.assertEqual(self.membership(label, change), {'acceptance-a': False, 'acceptance-b': False})

    def test_consistent_owners_admin_and_delegated_employee_keep_exact_scope(self):
        for label in ('acceptance-a', 'second', 'viewer', 'delegated'):
            with self.subTest(account=label):
                self.assertEqual(self.membership(label), {'acceptance-a': True, 'acceptance-b': False})

    def test_explicit_id_against_slug_denies_both_including_owner(self):
        self.assert_denied_both('acceptance-a', lambda user: setattr(user, 'tenant_id', self.accounts['acceptance-b']['tenant_id']))
        self.assert_denied_both('second', lambda user: setattr(user, 'tenant_slug', 'acceptance-b'))

    def test_ownership_and_legacy_owner_cannot_override_explicit_scope(self):
        def rebind_owner(user):
            user.tenant_id = self.accounts['acceptance-b']['tenant_id']
            user.tenant_slug = 'acceptance-b'
            user.municipio_id = self.accounts['acceptance-b']['id']
        self.assert_denied_both('acceptance-a', rebind_owner)
        self.assert_denied_both('second', lambda user: setattr(user, 'municipio_id', self.accounts['acceptance-b']['id']))

    def test_legacy_employee_owner_resolution_is_supported_and_invalid_owner_denied(self):
        def legacy(user):
            user.tenant_id = None
            user.tenant_slug = None
            user.empresa_id = None
        self.assertEqual(self.membership('viewer', legacy), {'acceptance-a': True, 'acceptance-b': False})
        def invalid(user):
            legacy(user)
            user.municipio_id = 999999
        self.assert_denied_both('viewer', invalid)

    def test_invalid_explicit_slug_has_no_legacy_fallback(self):
        self.assert_denied_both('acceptance-a', lambda user: setattr(user, 'tenant_slug', 'missing-tenant'))

    def test_superadmin_requires_configured_allowlist(self):
        from database import db
        from models import User
        def platform_role(user):
            user.rol = 'super_admin'
        with self.app.app_context():
            email = db.session.get(User, self.accounts['second']['id']).email
        with patch.dict('os.environ', {'CLERK_SUPERADMIN_EMAILS': email}):
            self.assertEqual(self.membership('second', platform_role), {'acceptance-a': True, 'acceptance-b': True})
        with patch.dict('os.environ', {'CLERK_SUPERADMIN_EMAILS': 'other@example.invalid'}):
            self.assertEqual(self.membership('second', platform_role), {'acceptance-a': True, 'acceptance-b': False})

    def analytics_membership(self, label, change=None):
        from database import db
        from models import User
        from services.analytics.rbac import _viewer_from_user
        with self.app.app_context():
            user = db.session.get(User, self.accounts[label]['id'])
            if change:
                change(user)
            viewer = _viewer_from_user(user)
            result = {
                slug: (
                    viewer.can_access(str(self.accounts[slug]['tenant_id']), 'profile'),
                    viewer.can_access(str(self.accounts[slug]['id']), 'owner'),
                ) for slug in ('acceptance-a', 'acceptance-b')
            }
            db.session.rollback()
            return result

    def test_analytics_profile_and_owner_namespaces_keep_consistent_membership(self):
        for label in ('acceptance-a', 'second', 'viewer', 'delegated'):
            with self.subTest(account=label):
                self.assertEqual(self.analytics_membership(label), {
                    'acceptance-a': (True, True), 'acceptance-b': (False, False)})
        def legacy(user):
            user.tenant_id = None
            user.tenant_slug = None
            user.empresa_id = None
        self.assertEqual(self.analytics_membership('viewer', legacy), {
            'acceptance-a': (True, True), 'acceptance-b': (False, False)})

    def test_analytics_conflicting_membership_denies_both_namespaces(self):
        conflicts = [
            ('acceptance-a', lambda user: setattr(user, 'tenant_id', self.accounts['acceptance-b']['tenant_id'])),
            ('second', lambda user: setattr(user, 'tenant_slug', 'acceptance-b')),
            ('second', lambda user: setattr(user, 'municipio_id', self.accounts['acceptance-b']['id'])),
            ('viewer', lambda user: setattr(user, 'municipio_id', 999999)),
            ('acceptance-a', lambda user: setattr(user, 'tenant_slug', 'missing-tenant')),
        ]
        for index, (label, change) in enumerate(conflicts):
            with self.subTest(account=label, change=index):
                self.assertEqual(self.analytics_membership(label, change), {
                    'acceptance-a': (False, False), 'acceptance-b': (False, False)})

    def test_normal_login_provider_status_and_foreign_scope_use_actual_routes(self):
        from database import db
        from models import User
        for label in ('acceptance-a', 'viewer', 'delegated'):
            with self.subTest(account=label):
                client = self.app.test_client()
                login = client.post('/auth/login', json={'email': self.accounts[label]['email'], 'password': self.password})
                self.assertEqual(login.status_code, 200)
                profile = client.get('/api/me')
                self.assertEqual(profile.status_code, 200)
                self.assertEqual(profile.get_json()['tenant_slug'], 'acceptance-a')
                own = client.get('/api/v2/tenants/acceptance-a/integrations/whatsapp/status')
                self.assertEqual(own.status_code, 200, own.get_json())
                foreign = client.get('/api/v2/tenants/acceptance-b/integrations/whatsapp/status')
                self.assertEqual(foreign.status_code, 403, foreign.get_json())
                self.assertEqual(foreign.get_json()['reason_code'], 'forbidden_tenant')
                own_analytics = client.get('/admin/bot/settings', query_string={'tenant_id': self.accounts['acceptance-a']['tenant_id']})
                self.assertEqual(own_analytics.status_code, 200, own_analytics.get_json())
                foreign_analytics = client.get('/admin/bot/settings', query_string={'tenant_id': self.accounts['acceptance-b']['tenant_id']})
                self.assertEqual(foreign_analytics.status_code, 403, foreign_analytics.get_json())
                for section in ('config', 'orders'):
                    own_section = client.get(f'/api/admin/tenants/acceptance-a/{section}')
                    self.assertEqual(own_section.status_code, 200, own_section.get_json())
                    foreign_section = client.get(f'/api/admin/tenants/acceptance-b/{section}')
                    self.assertEqual(foreign_section.status_code, 403, foreign_section.get_json())
        client = self.app.test_client()
        self.assertEqual(client.post('/auth/login', json={'email': self.accounts['second']['email'], 'password': self.password}).status_code, 200)
        with self.app.app_context():
            user = db.session.get(User, self.accounts['second']['id'])
            user.tenant_slug = 'acceptance-b'
            db.session.commit()
        try:
            for slug in ('acceptance-a', 'acceptance-b'):
                denied = client.get(f'/api/v2/tenants/{slug}/integrations/whatsapp/status')
                self.assertEqual(denied.status_code, 403, denied.get_json())
                self.assertEqual(denied.get_json()['reason_code'], 'forbidden_tenant')
                denied_analytics = client.get('/admin/bot/settings', query_string={'tenant_id': self.accounts[slug]['tenant_id']})
                self.assertEqual(denied_analytics.status_code, 403, denied_analytics.get_json())
                for section in ('config', 'orders'):
                    denied_section = client.get(f'/api/admin/tenants/{slug}/{section}')
                    self.assertEqual(denied_section.status_code, 403, denied_section.get_json())
        finally:
            with self.app.app_context():
                db.session.get(User, self.accounts['second']['id']).tenant_slug = 'acceptance-a'
                db.session.commit()

    def test_owner_cannot_read_config_or_orders_after_conflicting_explicit_rebind(self):
        from database import db
        from models import User
        client = self.app.test_client()
        self.assertEqual(client.post('/auth/login', json={
            'email': self.accounts['acceptance-a']['email'], 'password': self.password}).status_code, 200)
        with self.app.app_context():
            owner = db.session.get(User, self.accounts['acceptance-a']['id'])
            owner.tenant_id = self.accounts['acceptance-b']['tenant_id']
            owner.tenant_slug = 'acceptance-b'
            owner.municipio_id = self.accounts['acceptance-b']['id']
            db.session.commit()
        try:
            for slug in ('acceptance-a', 'acceptance-b'):
                for section in ('config', 'orders'):
                    denied = client.get(f'/api/admin/tenants/{slug}/{section}')
                    self.assertEqual(denied.status_code, 403, denied.get_json())
        finally:
            with self.app.app_context():
                owner = db.session.get(User, self.accounts['acceptance-a']['id'])
                owner.tenant_id = self.accounts['acceptance-a']['tenant_id']
                owner.tenant_slug = 'acceptance-a'
                owner.municipio_id = owner.id
                db.session.commit()

    def test_legacy_owner_id_is_not_an_unrelated_tenant_profile_id(self):
        from database import db
        from models import User
        client = self.app.test_client()
        self.assertEqual(client.post('/auth/login', json={
            'email': self.accounts['viewer']['email'], 'password': self.password}).status_code, 200)
        with self.app.app_context():
            viewer = db.session.get(User, self.accounts['viewer']['id'])
            viewer.tenant_id = None
            viewer.tenant_slug = None
            # B's profile ID belongs to a different numeric namespace. No
            # tenant is owned by this ID, so it cannot grant legacy membership.
            viewer.municipio_id = self.accounts['acceptance-b']['tenant_id']
            db.session.commit()
        try:
            for slug in ('acceptance-a', 'acceptance-b'):
                for section in ('config', 'orders'):
                    denied = client.get(f'/api/admin/tenants/{slug}/{section}')
                    self.assertEqual(denied.status_code, 403, denied.get_json())
        finally:
            with self.app.app_context():
                viewer = db.session.get(User, self.accounts['viewer']['id'])
                viewer.tenant_id = self.accounts['acceptance-a']['tenant_id']
                viewer.tenant_slug = 'acceptance-a'
                viewer.municipio_id = self.accounts['acceptance-a']['id']
                db.session.commit()


if __name__ == '__main__':
    unittest.main()
