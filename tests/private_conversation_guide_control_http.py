"""Real Flask/auth/database acceptance, isolated from all customer environments."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__': prepare_process()
from copy import deepcopy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

class GuideControlHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        from database import db
        from models import User
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-guide-control-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        with cls.app.app_context():
            root = User(name='QA platform administrator', email='root@example.invalid',
                        rol='super_admin', email_verified=True, acepto_terminos=True)
            root.set_password(cls.password)
            db.session.add(root); db.session.commit()
            cls.accounts['root'] = {'id': root.id, 'email': root.email}
        cls.allowlist = patch.dict(os.environ, {'CLERK_SUPERADMIN_EMAILS': 'root@example.invalid'})
        cls.allowlist.start()

    @classmethod
    def tearDownClass(cls):
        from database import db
        cls.allowlist.stop()
        with cls.app.app_context(): db.session.remove(); db.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        from database import db
        from models import TenantProfile, AuditEvent
        with self.app.app_context():
            for key in ('acceptance-a', 'acceptance-b'):
                tenant = db.session.get(TenantProfile, self.accounts[key]['tenant_id'])
                tenant.configuracion = {'preserved': {'value': 1}}
                tenant.is_active = True
            AuditEvent.query.filter(AuditEvent.event_type.like('conversation_guide.%')).delete()
            db.session.commit()
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False

    def login(self, name='root'):
        client = self.app.test_client()
        result = client.post('/auth/login', json={'email': self.accounts[name]['email'], 'password': self.password})
        self.assertEqual(result.status_code, 200, result.get_json())
        return client

    def url(self, slug='acceptance-a'):
        return f'/api/admin/tenants/{slug}/conversation-guide-control'

    def read(self, client):
        result = client.get(self.url())
        self.assertEqual(result.status_code, 200, result.get_json())
        return result.get_json()

    def command(self, control, enabled=True):
        artifact = control['installed_guide']
        value = {'contract_version': 'tenant.conversation_guide_control_command.v1',
                 'tenant': control['tenant'], 'expected_revision': control['revision'],
                 'guide_id': 'accessible-support-evaluation', 'enabled': enabled,
                 'acknowledge_evaluation_only': True}
        if enabled:
            value.update(expected_guide_sha256=artifact['guide_sha256'],
                         expected_source_sha256=artifact['source']['sha256'])
        return value

    def put(self, client, body, **kwargs):
        return client.put(self.url(), json=body, headers={'X-Chatboc-Guide-Control': '1'}, **kwargs)

    def snapshot(self):
        from database import db
        from models import TenantProfile, AuditEvent
        with self.app.app_context():
            configs = {key: deepcopy(db.session.get(TenantProfile, self.accounts[key]['tenant_id']).configuracion)
                       for key in ('acceptance-a', 'acceptance-b')}
            audits = AuditEvent.query.filter(AuditEvent.event_type.like('conversation_guide.%')).order_by(AuditEvent.id).all()
            return configs, [deepcopy(row.details) for row in audits]

    def test_control_read_is_private_and_does_not_enable_or_audit(self):
        client = self.login(); before = self.snapshot()
        response = client.get(self.url()); control = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertIn('Authorization', response.headers['Vary'])
        self.assertFalse(control['state']['enabled']); self.assertTrue(control['can_enable'])
        self.assertEqual(control['installed_guide']['node_count'], 29)
        self.assertNotIn('nodes', control['installed_guide'])
        self.assertEqual(self.snapshot(), before)

    def test_enable_then_disable_revokes_the_same_logged_in_reader(self):
        admin = self.login(); reader = self.login('acceptance-a')
        target = '/api/admin/tenants/acceptance-a/conversation-guide'
        self.assertEqual(reader.get(target).status_code, 404)
        response = self.put(admin, self.command(self.read(admin)))
        self.assertEqual(response.status_code, 200, response.get_json())
        receipt = response.get_json(); self.assertTrue(receipt['saved'])
        self.assertTrue(receipt['control']['state']['enabled'])
        self.assertEqual(reader.get(target).status_code, 200)
        self.assertEqual(reader.get('/auth/me').get_json()['channel_activation']['organization_setup']['conversation_guide']['tenant']['slug'], 'acceptance-a')
        off = self.put(admin, self.command(receipt['control'], False))
        self.assertEqual(off.status_code, 200); self.assertEqual(reader.get(target).status_code, 404)
        configs, audits = self.snapshot()
        self.assertEqual(configs['acceptance-a']['preserved'], {'value': 1})
        self.assertEqual(configs['acceptance-b'], {'preserved': {'value': 1}})
        self.assertEqual([a['enabled'] for a in audits], [True, False])
        self.assertTrue(all(a['evaluation_only'] and not a['provider_calls_performed'] for a in audits))

    def test_only_platform_admin_may_change_evaluation_access(self):
        admin = self.login(); body = self.command(self.read(admin)); before = self.snapshot()
        for actor in (None, 'acceptance-a', 'second', 'delegated', 'viewer', 'acceptance-b'):
            with self.subTest(actor=actor):
                client = self.login(actor) if actor else self.app.test_client()
                self.assertEqual(self.put(client, body).status_code, 401 if actor is None else 403)
        self.assertEqual(self.snapshot(), before)

    def test_authorized_tenant_admin_may_read_but_cannot_enable(self):
        for actor in ('acceptance-a', 'second', 'delegated'):
            with self.subTest(actor=actor):
                control = self.read(self.login(actor))
                self.assertFalse(control['can_enable']); self.assertFalse(control['can_disable'])
        for actor in ('viewer', 'acceptance-b'):
            self.assertEqual(self.login(actor).get(self.url()).status_code, 403)

    def test_stale_revision_and_duplicate_request_do_not_repeat_audit(self):
        client = self.login(); body = self.command(self.read(client))
        self.assertEqual(self.put(client, body).status_code, 200)
        before = self.snapshot()
        self.assertEqual(self.put(client, body).status_code, 412)
        self.assertEqual(self.snapshot(), before)
        no_change = self.put(client, self.command(self.read(client)))
        self.assertEqual(no_change.status_code, 200)
        self.assertFalse(no_change.get_json()['saved']); self.assertEqual(self.snapshot(), before)

    def test_invalid_payloads_never_change_configuration(self):
        client = self.login(); original = self.command(self.read(client)); before = self.snapshot()
        for field, value in [('tenant', {'id': 999, 'slug': 'acceptance-a'}),
            ('tenant', {'id': True, 'slug': 'acceptance-a'}), ('tenant', {'id': self.accounts['acceptance-a']['tenant_id'], 'slug': 'acceptance-b'}),
            ('enabled', 1), ('enabled', 'true'), ('acknowledge_evaluation_only', False),
            ('guide_id', '../other'), ('guide_id', 'https://example.invalid/source'),
            ('contract_version', 'unknown'), ('expected_revision', ''),
            ('expected_guide_sha256', '0'*64), ('expected_source_sha256', '0'*64), ('extra', True)]:
            with self.subTest(field=field, value=value):
                response = self.put(client, {**original, field: value})
                self.assertIn(response.status_code, (400, 412, 428))
                self.assertEqual(self.snapshot(), before)

    def test_missing_fields_duplicate_json_and_form_data_are_rejected(self):
        client = self.login(); body = self.command(self.read(client)); before = self.snapshot()
        for field in ('tenant', 'expected_revision', 'guide_id', 'enabled', 'acknowledge_evaluation_only'):
            invalid = deepcopy(body); invalid.pop(field)
            self.assertIn(self.put(client, invalid).status_code, (400, 428))
        headers = {'X-Chatboc-Guide-Control': '1'}
        self.assertEqual(client.put(self.url(), data='{"enabled":true,"enabled":false}', content_type='application/json', headers=headers).status_code, 400)
        self.assertEqual(client.put(self.url(), data='x=1', headers=headers).status_code, 415)
        self.assertEqual(client.put(self.url(), json=body).status_code, 415)
        self.assertEqual(client.put(self.url(), data=' '*8193, content_type='application/json', headers=headers).status_code, 413)
        self.assertEqual(self.snapshot(), before)

    def test_route_headers_queries_and_aliases_cannot_change_scope(self):
        client = self.login(); before = self.snapshot()
        for url in (self.url('missing'), self.url('guide-alias')):
            with patch.dict(self.app.config, {'TENANT_ALIASES': {'guide-alias': 'acceptance-a'}}):
                self.assertEqual(client.get(url).status_code, 404)
        for query in ('tenant=acceptance-b', 'tenant_id=999', 'tenant=acceptance-a&tenant=acceptance-a', 'guide_id=other'):
            self.assertEqual(client.get(self.url()+'?'+query).status_code, 400)
        for header, value in [('X-Tenant', 'acceptance-b'), ('X-Tenant-Slug', 'acceptance-b'), ('X-Tenant-ID', '999')]:
            self.assertEqual(client.get(self.url(), headers={header: value}).status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_uninstalled_artifact_cannot_enable_but_can_always_be_disabled(self):
        client = self.login(); body = self.command(self.read(client))
        with patch('services.tenant_conversation_guide_control.load_guide', side_effect=OSError('PRIVATE PATH')):
            self.assertFalse(self.read(client)['can_enable'])
            response = self.put(client, body); self.assertEqual(response.status_code, 503)
            self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertEqual(self.put(client, body).status_code, 200)
        with patch('services.tenant_conversation_guide_control.load_guide', side_effect=ValueError('digest mismatch')):
            control = self.read(client); self.assertTrue(control['can_disable'])
            self.assertEqual(self.put(client, self.command(control, False)).status_code, 200)
        self.assertEqual(self.login('acceptance-a').get('/api/admin/tenants/acceptance-a/conversation-guide').status_code, 404)

    def test_audit_failure_rolls_back_configuration(self):
        from database import db
        from sqlalchemy.exc import SQLAlchemyError
        client = self.login(); body = self.command(self.read(client)); before = self.snapshot()
        with patch.object(db.session, 'flush', side_effect=SQLAlchemyError('private database error')):
            response = self.put(client, body)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private database', response.get_data(as_text=True))
        self.assertEqual(self.snapshot(), before)

    def test_cutover_fence_blocks_mutations_but_allows_status(self):
        client = self.login(); body = self.command(self.read(client)); before = self.snapshot()
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        control = self.read(client)
        self.assertFalse(control['can_enable']); self.assertFalse(control['can_disable'])
        self.assertEqual(self.put(client, body).status_code, 503)
        self.assertEqual(self.snapshot(), before)

    def test_privileged_identity_is_rechecked_after_login(self):
        client = self.login(); body = self.command(self.read(client)); before = self.snapshot()
        with patch.dict(os.environ, {'CLERK_SUPERADMIN_EMAILS': 'another@example.invalid'}):
            self.assertIn(self.put(client, body).status_code, (401, 403))
        self.assertEqual(self.snapshot(), before)

    def test_disable_does_not_approve_content_or_modify_other_settings(self):
        client = self.login(); initial = self.read(client)
        noop = self.put(client, self.command(initial, False))
        self.assertEqual(noop.status_code, 200); self.assertFalse(noop.get_json()['saved'])
        on = self.put(client, self.command(initial)).get_json()['control']
        off = self.put(client, self.command(on, False)).get_json()['control']
        self.assertNotEqual(initial['revision'], off['revision'])
        self.assertEqual(off['state']['version'], 2)
        self.assertFalse(off['operational_content_approved'])
        self.assertEqual(self.put(client, self.command(initial)).status_code, 412)

    def test_retiring_the_tenant_prevents_later_activation(self):
        from database import db
        from models import TenantProfile
        client = self.login(); body = self.command(self.read(client))
        with self.app.app_context():
            db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id']).is_active = False
            db.session.commit()
        before = self.snapshot()
        self.assertIn(self.put(client, body).status_code, (401, 403))
        self.assertEqual(self.snapshot(), before)

    def test_post_patch_and_delete_do_not_invoke_control_write(self):
        client = self.login(); before = self.snapshot()
        for method in ('POST', 'PATCH', 'DELETE'):
            self.assertEqual(client.open(self.url(), method=method, json={}).status_code, 405)
        self.assertEqual(self.snapshot(), before)

if __name__ == '__main__': unittest.main()
