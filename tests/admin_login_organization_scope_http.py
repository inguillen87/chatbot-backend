"""Real native admin login over HTTP with a scoped organization access.

A scoped access (own domain such as an organization subdomain, or /t/<slug>/login)
is exclusive to that organization. Disposable SQLite and synthetic accounts only;
no provider, remote credential or production data is used.
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

import tempfile
import unittest


class AdminLoginOrganizationScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-admin-login-scope-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        cls._add_unresolvable_accounts()

    @classmethod
    def _add_unresolvable_accounts(cls):
        # Accounts the strong resolver rejects (resolve_consistent_user_tenant -> None) but
        # whose stored tenant_slug equals a scope: contradictory and orphan associations.
        from database import db
        from models import TenantProfile, User
        with cls.app.app_context():
            tenant_a = TenantProfile.query.filter_by(slug='acceptance-a').one()
            tenant_b = TenantProfile.query.filter_by(slug='acceptance-b').one()
            rows = {
                # Membership points to B, slug claims A.
                'contradictory': dict(tenant_id=tenant_b.id, tenant_slug='acceptance-a', municipio_id=None),
                # Slug claims A, legacy owner reference points to B's owner.
                'legacy-conflict': dict(tenant_id=None, tenant_slug='acceptance-a', municipio_id=tenant_b.municipio_id),
                # Slug names an organization that does not exist.
                'orphan': dict(tenant_id=None, tenant_slug='organizacion-borrada', municipio_id=None),
            }
            for label, fields in rows.items():
                user = User(name=label, email=label + '@example.invalid', rol='admin', tipo_chat='municipio', **fields)
                user.set_password(cls.password)
                db.session.add(user); db.session.flush()
                cls.accounts[label] = {'id': user.id, 'email': user.email}
            db.session.commit()
            from utils.tenant_admin_access import resolve_consistent_user_tenant
            for label in rows:
                assert resolve_consistent_user_tenant(db.session.get(User, cls.accounts[label]['id'])) is None, label

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def login(self, account, scope=..., password=None):
        payload = {'email': self.accounts[account]['email'], 'password': password or self.password}
        if scope is not ...:
            payload['tenant_slug'] = scope
        return self.app.test_client().post('/api/auth/admin/login', json=payload)

    def assert_denied_without_token(self, response, status, reason):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body.get('reason_code'), reason)
        self.assertNotIn('token', body)
        self.assertNotIn('user', body)

    def test_member_enters_its_own_organization_access(self):
        for account in ('acceptance-a', 'second'):
            with self.subTest(account=account):
                response = self.login(account, 'acceptance-a')
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                body = response.get_json()
                self.assertTrue(body['token'])
                self.assertEqual(body['user']['tenant_slug'], 'acceptance-a')

    def test_scope_is_compared_case_and_space_insensitively(self):
        response = self.login('acceptance-a', '  Acceptance-A ')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_account_of_another_organization_never_receives_a_token(self):
        self.assert_denied_without_token(self.login('acceptance-b', 'acceptance-a'), 403, 'organization_mismatch')
        self.assert_denied_without_token(self.login('second', 'acceptance-b'), 403, 'organization_mismatch')

    def test_unresolvable_account_never_enters_through_its_stored_slug(self):
        for account, scope in (('contradictory', 'acceptance-a'), ('legacy-conflict', 'acceptance-a'), ('orphan', 'organizacion-borrada')):
            with self.subTest(account=account):
                self.assert_denied_without_token(self.login(account, scope), 403, 'organization_mismatch')

    def test_unknown_organization_is_denied_like_a_foreign_one(self):
        self.assert_denied_without_token(self.login('acceptance-a', 'organizacion-inexistente'), 403, 'organization_mismatch')

    def test_wrong_password_is_reported_before_the_scope(self):
        response = self.login('acceptance-b', 'acceptance-a', password='not-the-password')
        self.assertEqual(response.status_code, 401)
        body = response.get_json()
        self.assertNotIn('reason_code', body)
        self.assertNotIn('token', body)

    def test_malformed_scope_is_rejected_before_any_lookup(self):
        for scope in ('', '   ', 7, ['acceptance-a'], {'slug': 'acceptance-a'}, 'a' * 129):
            with self.subTest(scope=scope):
                self.assert_denied_without_token(self.login('acceptance-a', scope), 400, 'invalid_login_scope')

    def test_central_access_without_scope_is_unchanged(self):
        for account, slug in (('acceptance-a', 'acceptance-a'), ('acceptance-b', 'acceptance-b')):
            with self.subTest(account=account):
                response = self.login(account)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual(response.get_json()['user']['tenant_slug'], slug)
        explicit_null = self.app.test_client().post('/api/auth/admin/login', json={
            'email': self.accounts['acceptance-b']['email'], 'password': self.password, 'tenant_slug': None})
        self.assertEqual(explicit_null.status_code, 200, explicit_null.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
