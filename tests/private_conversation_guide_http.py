"""Private guide acceptance with real Flask auth and disposable SQLite only."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__':
    prepare_process()

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

WRITE_FIXTURE = False


class PrivateConversationGuideHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-private-guide-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        self.configure({'enabled': True, 'guide_id': 'accessible-support-evaluation'})

    def configure(self, value, *, active=True):
        from database import db
        from models import TenantProfile
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
            tenant.configuracion = {'private_conversation_guide': value, 'preserved': {'value': 1}}
            tenant.is_active = active
            db.session.commit()

    def login(self, account='acceptance-a'):
        client = self.app.test_client()
        response = client.post('/auth/login', json={
            'email': self.accounts[account]['email'], 'password': self.password})
        self.assertEqual(response.status_code, 200, response.get_json())
        return client

    def path(self, slug='acceptance-a'):
        return f'/api/admin/tenants/{slug}/conversation-guide'

    def test_pinned_bytes_and_all_29_nodes_are_reused(self):
        from services.accessible_support_guide import PATH, GUIDE_SHA256, load_guide
        guide = load_guide()
        self.assertEqual(sha256(PATH.read_bytes()).hexdigest(), GUIDE_SHA256)
        self.assertEqual(GUIDE_SHA256, 'f028f657752ecc74c9cb1d7f4ae8408d210f42a9b0d8ccb6a9afc7bf83d4bf41')
        self.assertEqual(len(guide['nodes']), 29)
        client = self.login()
        for key, node in guide['nodes'].items():
            with self.subTest(node=key):
                response = client.get(self.path(), query_string={'node': key})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()['menu'], node)
                for action in node['actions']:
                    response = client.get(self.path(), query_string={'node': key, 'selection': action['code']})
                    self.assertEqual(response.get_json()['menu'], guide['nodes'][action['target']])

    def test_source_policy_and_discovery_contract_export(self):
        client = self.login()
        response = client.get(self.path())
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertIn('Authorization', response.headers['Vary'])
        self.assertEqual(body['contract_version'], 'tenant.conversation_guide.v1')
        self.assertEqual(body['tenant'], {'id': self.accounts['acceptance-a']['tenant_id'], 'slug': 'acceptance-a'})
        self.assertTrue(body['evaluation_only'])
        self.assertEqual(body['source']['page_count'], 14)
        self.assertEqual(body['source']['sha256'], '1f6de63d4f70ede4e967077cc9eae768c64574b022d66c5ebb8868aac3d4a6ee')
        self.assertEqual(body['source']['approval_status'], 'institutional_validation_required')
        self.assertTrue(all(value is False for value in body['policy'].values()))
        self.assertFalse(body['writes_performed'])
        self.assertFalse(body['provider_calls_performed'])
        with patch('services.tenant_conversation_guide.load_guide', side_effect=AssertionError('discovery must not read content')):
            config = client.get('/api/admin/tenants/acceptance-a/config')
        self.assertEqual(config.status_code, 200)
        access = config.get_json()['conversation_guide']
        self.assertEqual(access['contract_version'], 'tenant.conversation_guide_access.v1')
        self.assertEqual(access['endpoint'], self.path())
        self.assertEqual(access['tenant'], body['tenant'])
        self.assertEqual(access['ui'], body['ui'])
        self.assertNotIn('menu', access)
        self.assertNotIn('source', access)
        if WRITE_FIXTURE:
            path = Path(__file__).resolve().parents[2] / 'frontend/tests/fixtures/private-conversation-guide.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'access': access, 'guide': body}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def test_discovery_uses_actor_in_profile_and_channel_activation(self):
        for account, enabled in (('acceptance-a', True), ('delegated', True), ('viewer', False), ('acceptance-b', False)):
            with self.subTest(account=account):
                client = self.login(account)
                slug = 'acceptance-b' if account == 'acceptance-b' else 'acceptance-a'
                response = client.get(f'/api/v2/tenants/{slug}/activation/channels')
                self.assertEqual(response.status_code, 200)
                access = response.get_json()['organization_setup']['conversation_guide']
                self.assertEqual(access is not None, enabled)
                profile = client.get('/auth/me')
                self.assertEqual(profile.status_code, 200)
                data = profile.get_json()
                self.assertEqual(data['channel_activation']['organization_setup']['conversation_guide'] is not None, enabled)

    def test_actorless_builder_does_not_disclose_guide_or_change_readiness(self):
        from database import db
        from models import TenantProfile, User
        from services.channel_activation import build_channel_activation_payload
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
            owner = db.session.get(User, self.accounts['acceptance-a']['id'])
            with patch('services.tenant_conversation_guide.load_guide', side_effect=AssertionError('discovery must not read content')):
                public = build_channel_activation_payload(tenant)
                private = build_channel_activation_payload(tenant, actor=owner)
            self.assertIsNone(public['organization_setup']['conversation_guide'])
            self.assertIsNotNone(private['organization_setup']['conversation_guide'])
            self.assertEqual(public['summary'], private['summary'])
            self.assertEqual(public['organization_setup']['summary'], private['organization_setup']['summary'])

    def test_anonymous_employee_and_foreign_admin_are_denied_before_content_read(self):
        for account, expected in ((None, 401), ('viewer', 403), ('acceptance-b', 403)):
            client = self.login(account) if account else self.app.test_client()
            with self.subTest(account=account), patch('routes.admin_tenant.guide_menu_payload', side_effect=AssertionError('content read forbidden')):
                self.assertEqual(client.get(self.path()).status_code, expected)

    def test_second_and_scoped_admin_can_read(self):
        for account in ('second', 'delegated'):
            with self.subTest(account=account):
                self.assertEqual(self.login(account).get(self.path()).status_code, 200)

    def test_absent_disabled_unknown_and_malformed_config_never_load_content(self):
        client = self.login()
        for value in (None, {}, False, 'enabled', {'enabled': True}, {'enabled': 1, 'guide_id': 'accessible-support-evaluation'},
                      {'enabled': False, 'guide_id': 'accessible-support-evaluation'}, {'enabled': True, 'guide_id': 'unknown'},
                      {'enabled': True, 'guide_id': 'accessible-support-evaluation', 'path': '../other.json'}):
            self.configure(value)
            with self.subTest(value=value), patch('routes.admin_tenant.guide_menu_payload', side_effect=AssertionError('content read forbidden')):
                self.assertEqual(client.get(self.path()).status_code, 404)
            response = client.get('/api/admin/tenants/acceptance-a/config')
            self.assertIsNone(response.get_json()['conversation_guide'])

    def test_existing_session_loses_access_when_disabled_or_inactive(self):
        client = self.login()
        self.assertEqual(client.get(self.path()).status_code, 200)
        self.configure({'enabled': False, 'guide_id': 'accessible-support-evaluation'})
        self.assertEqual(client.get(self.path()).status_code, 404)
        self.configure({'enabled': True, 'guide_id': 'accessible-support-evaluation'}, active=False)
        self.assertIn(client.get(self.path()).status_code, (401, 403))

    def test_unknown_url_does_not_fall_back_to_current_or_aliased_tenant(self):
        client = self.login()
        with patch.dict(self.app.config, {'TENANT_ALIASES': {'guide-alias': 'acceptance-a'}}):
            for slug in ('missing', 'guide-alias'):
                response = client.get(self.path(slug), headers={'X-Tenant-Slug': 'acceptance-a'})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(client.get(self.path('acceptance-b')).status_code, 403)

    def test_scope_hints_must_match_and_unknown_queries_are_rejected(self):
        client = self.login()
        response = client.get(self.path(), query_string={'tenant': 'acceptance-a', 'tenant_slug': 'acceptance-a',
                              'tenant_id': str(self.accounts['acceptance-a']['tenant_id']), 'node': 'main'})
        self.assertEqual(response.status_code, 200)
        for query in ('tenant=acceptance-b', 'tenant_slug=acceptance-b', 'tenant_id=999', 'guide_id=other',
                      'node=start&node=main', 'selection=1&selection=2', 'tenant=acceptance-a&tenant=acceptance-a'):
            with self.subTest(query=query):
                self.assertEqual(client.get(self.path() + '?' + query).status_code, 400)
        for header, value in (('X-Tenant-Slug', 'acceptance-b'), ('X-Tenant', 'acceptance-b'), ('X-Tenant-ID', '999')):
            self.assertEqual(client.get(self.path(), headers={header: value}).status_code, 400)

    def test_invalid_navigation_and_mutating_methods_fail_closed(self):
        client = self.login()
        for query in ({'node': '../main'}, {'node': ''}, {'selection': ''}, {'selection': 'x' * 17}, {'selection': '1234'}):
            self.assertEqual(client.get(self.path(), query_string=query).status_code, 400)
        for method in ('post', 'put', 'patch', 'delete'):
            self.assertEqual(getattr(client, method)(self.path(), json={'node': 'start'}).status_code, 405)
        self.assertEqual(client.get(self.path(), query_string={'node': 'thanks', 'selection': 'inicio'}).get_json()['menu']['id'], 'start')
        self.assertEqual(client.get(self.path(), query_string={'selection': 'MENU'}).get_json()['menu']['id'], 'main')

    def test_navigation_never_writes_or_creates_an_implicit_entity_token(self):
        from database import db
        from models import TenantProfile, User
        from sqlalchemy import event
        client = self.login()
        with self.app.app_context():
            owner = db.session.get(User, self.accounts['acceptance-a']['id'])
            owner.entity_token = None
            db.session.commit()
            tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
            before = deepcopy(tenant.configuracion)
            engine = db.engine
        writes = []
        def watch(_connection, _cursor, statement, _params, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER', 'DROP'}:
                writes.append(statement.split(None, 1)[0])
        event.listen(engine, 'before_cursor_execute', watch)
        try:
            for node in ('start', 'human', 'handoff-example', 'feedback-resolution', 'feedback-ease', 'thanks'):
                self.assertEqual(client.get(self.path(), query_string={'node': node}).status_code, 200)
        finally:
            event.remove(engine, 'before_cursor_execute', watch)
        self.assertEqual(writes, [])
        with self.app.app_context():
            self.assertIsNone(db.session.get(User, self.accounts['acceptance-a']['id']).entity_token)
            self.assertEqual(db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id']).configuracion, before)

    def test_corrupted_or_missing_artifact_is_unavailable_without_content(self):
        client = self.login()
        for error in (ValueError('evaluation_guide_digest_mismatch'), OSError('not readable')):
            with patch('services.tenant_conversation_guide.load_guide', side_effect=error):
                response = client.get(self.path())
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json(), {'error': 'conversation_guide_unavailable'})


if __name__ == '__main__':
    WRITE_FIXTURE = '--write-fixture' in sys.argv
    if WRITE_FIXTURE:
        sys.argv.remove('--write-fixture')
    unittest.main()
