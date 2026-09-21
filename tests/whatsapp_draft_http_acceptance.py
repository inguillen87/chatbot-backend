"""Standalone full-app WhatsApp draft acceptance on disposable identities/SQLite.

Run python -m tests.whatsapp_draft_http_acceptance. Never targets deployed data.
"""
from tests.profile_acceptance_runtime import prepare_process, create_disposable_app

if __name__ == '__main__':
    prepare_process()

import secrets
import tempfile
import threading
import unittest
from hashlib import sha256
from tests.profile_http_acceptance import BrowserSession

CATALOG = '/api/admin/whatsapp/template-packs'
EVENT = 'whatsapp_template_pack.local_drafts_materialized'


class WhatsAppDraftHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-whatsapp-drafts-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)
        from werkzeug.serving import make_server
        cls.server = make_server('127.0.0.1', 0, cls.app, threaded=True)
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        from database import db
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from database import db
        from models import AuditEvent, MessageTemplateRegistry, TenantProfile
        # Only the synthetic organizations created by this isolated process.
        self.ids = [self.accounts[name]['tenant_id'] for name in ('acceptance-a', 'acceptance-b')]
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False
        with self.app.app_context():
            AuditEvent.query.filter(AuditEvent.tenant_id.in_(self.ids), AuditEvent.event_type == EVENT).delete(synchronize_session=False)
            MessageTemplateRegistry.query.filter(MessageTemplateRegistry.tenant_id.in_(self.ids),
                MessageTemplateRegistry.provider == 'chatboc', MessageTemplateRegistry.channel == 'whatsapp').delete(synchronize_session=False)
            for tenant_id in self.ids:
                db.session.get(TenantProfile, tenant_id).is_active = True
            db.session.commit()

    def tearDown(self):
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False

    def login(self, name='acceptance-a'):
        browser = BrowserSession(self.origin)
        status, body, _ = browser.request('POST', '/auth/login', {
            'email': self.accounts[name]['email'], 'password': self.password})
        self.assertEqual(status, 200)
        self.assertTrue(body.get('token'))
        return browser

    def catalog(self, browser, slug='acceptance-a'):
        status, body, _ = browser.request('GET', f'{CATALOG}?tenant_slug={slug}')
        self.assertEqual(status, 200)
        self.assertEqual(body['tenant']['slug'], slug)
        self.assertFalse(body['provider_calls_performed'])
        return body

    def pack(self, browser, slug='acceptance-a', vertical='municipio'):
        return next(item for item in self.catalog(browser, slug)['packs'] if item['vertical'] == vertical)

    def save(self, browser, pack, key=None, slug='acceptance-a', body=None):
        key = key or 'acceptance:' + secrets.token_hex(16)
        return browser.request('POST', f'{CATALOG}/{pack["vertical"]}/drafts?tenant_slug={slug}',
            {'pack_version': pack['pack_version']} if body is None else body,
            headers={'Idempotency-Key': key})

    def stored(self, slug='acceptance-a'):
        from models import AuditEvent, MessageTemplateRegistry
        with self.app.app_context():
            tenant_id = self.accounts[slug]['tenant_id']
            rows = MessageTemplateRegistry.query.filter_by(tenant_id=tenant_id, provider='chatboc', channel='whatsapp').order_by(MessageTemplateRegistry.id).all()
            events = AuditEvent.query.filter_by(tenant_id=tenant_id, event_type=EVENT).order_by(AuditEvent.id).all()
            return ([{'id': row.id, 'name': row.name, 'status': row.status,
                'content_sid': row.content_sid, 'external_template_id': row.external_template_id,
                'metadata': row.metadata_json} for row in rows],
                [{'id': event.id, 'details': event.details} for event in events])

    def test_create_persists_drafts_and_one_audit_then_independent_session_reads(self):
        first, second = self.login(), self.login('second')
        pack = self.pack(first)
        key = 'acceptance:' + secrets.token_hex(16)
        status, receipt, _ = self.save(first, pack, key)
        self.assertEqual(status, 201)
        self.assertTrue(receipt['ok'])
        self.assertFalse(receipt['provider_calls_performed'])
        self.assertFalse(receipt['idempotent_replay'])
        self.assertEqual(receipt['idempotency']['key'], key)
        self.assertEqual(receipt['created_count'], len(pack['templates']))
        saved = self.pack(second)
        self.assertTrue(all(item['materialized'] for item in saved['templates']))
        self.assertTrue(all(item['lifecycle']['state'] == 'local_draft' for item in saved['templates']))
        self.assertTrue(all(item['lifecycle']['production_send_allowed'] is False for item in saved['templates']))
        rows, events = self.stored()
        self.assertEqual(len(rows), len(pack['templates']))
        self.assertEqual(len(events), 1)
        self.assertTrue(all(row['status'] == 'local_draft' and not row['content_sid'] and not row['external_template_id'] for row in rows))
        self.assertEqual(events[0]['details']['idempotency_key_hash'], sha256(key.encode()).hexdigest())
        self.assertNotIn('idempotency_key', events[0]['details'])
        self.assertFalse(events[0]['details']['provider_calls_performed'])

    def test_same_key_replays_without_duplicate_rows_or_audit(self):
        browser = self.login()
        pack = self.pack(browser)
        key = 'acceptance:' + secrets.token_hex(16)
        self.assertEqual(self.save(browser, pack, key)[0], 201)
        before = self.stored()
        status, receipt, _ = self.save(browser, pack, key)
        self.assertEqual(status, 200)
        self.assertTrue(receipt['idempotent_replay'])
        self.assertEqual(self.stored(), before)

    def test_different_operation_reuses_drafts_without_claiming_provider_approval(self):
        browser = self.login()
        pack = self.pack(browser)
        self.assertEqual(self.save(browser, pack)[0], 201)
        before, _ = self.stored()
        status, receipt, _ = self.save(browser, pack)
        self.assertEqual(status, 200)
        self.assertFalse(receipt['created'])
        self.assertFalse(receipt['idempotent_replay'])
        self.assertEqual(receipt['reused_count'], len(before))
        rows, events = self.stored()
        self.assertEqual([row['id'] for row in rows], [row['id'] for row in before])
        self.assertEqual(len(events), 2)
        self.assertFalse(receipt['provider_calls_performed'])

    def test_reused_key_for_different_vertical_is_rejected(self):
        browser = self.login()
        packs = self.catalog(browser)['packs']
        self.assertGreaterEqual(len(packs), 2)
        key = 'acceptance:' + secrets.token_hex(16)
        self.assertEqual(self.save(browser, packs[0], key)[0], 201)
        before = self.stored()
        self.assertEqual(self.save(browser, packs[1], key)[0], 409)
        self.assertEqual(self.stored(), before)

    def test_anonymous_cannot_read_or_create(self):
        browser = BrowserSession(self.origin)
        self.assertEqual(browser.request('GET', f'{CATALOG}?tenant_slug=acceptance-a')[0], 401)
        status, _, _ = browser.request('POST', f'{CATALOG}/municipio/drafts?tenant_slug=acceptance-a',
            {'pack_version': '1'}, headers={'Idempotency-Key': 'acceptance:anonymous'})
        self.assertEqual(status, 401)
        self.assertEqual(self.stored(), ([], []))

    def test_employee_can_read_but_not_create(self):
        browser = self.login('viewer')
        catalog = self.catalog(browser)
        self.assertTrue(catalog['capabilities']['read'])
        self.assertFalse(catalog['capabilities']['materialize_local_draft'])
        self.assertEqual(self.save(browser, catalog['packs'][0])[0], 403)
        self.assertEqual(self.stored(), ([], []))

    def test_foreign_session_cannot_read_or_write_another_organization(self):
        browser = self.login('acceptance-b')
        own_pack = self.pack(browser, 'acceptance-b')
        self.assertEqual(browser.request('GET', f'{CATALOG}?tenant_slug=acceptance-a')[0], 403)
        self.assertEqual(self.save(browser, own_pack, slug='acceptance-a')[0], 403)
        self.assertEqual(self.stored(), ([], []))
        self.assertEqual(self.stored('acceptance-b'), ([], []))

    def test_same_key_in_different_tenants_has_independent_records(self):
        first, second = self.login(), self.login('acceptance-b')
        key = 'acceptance:' + secrets.token_hex(16)
        pack_a, pack_b = self.pack(first), self.pack(second, 'acceptance-b')
        self.assertEqual(self.save(first, pack_a, key)[0], 201)
        self.assertEqual(self.save(second, pack_b, key, slug='acceptance-b')[0], 201)
        rows_a, events_a = self.stored()
        rows_b, events_b = self.stored('acceptance-b')
        self.assertTrue(set(row['id'] for row in rows_a).isdisjoint(row['id'] for row in rows_b))
        self.assertEqual((len(events_a), len(events_b)), (1, 1))
        self.assertNotEqual(events_a[0]['details']['request_fingerprint'], events_b[0]['details']['request_fingerprint'])

    def test_invalid_version_or_mixed_payload_does_not_write(self):
        browser = self.login()
        pack = self.pack(browser)
        self.assertEqual(self.save(browser, pack, body={'pack_version': 'not-the-current-version'})[0], 409)
        self.assertEqual(self.save(browser, pack, body={'pack_version': pack['pack_version'], 'submit_for_approval': True})[0], 400)
        status, _, _ = browser.request('POST', f'{CATALOG}/{pack["vertical"]}/drafts?tenant_slug=acceptance-a',
            {'pack_version': pack['pack_version']})
        self.assertEqual(status, 400)
        self.assertEqual(self.stored(), ([], []))

    def test_inactive_organization_cannot_materialize(self):
        from database import db
        from models import TenantProfile
        browser = self.login()
        pack = self.pack(browser)
        with self.app.app_context():
            db.session.get(TenantProfile, self.ids[0]).is_active = False
            db.session.commit()
        self.assertEqual(self.save(browser, pack)[0], 403)
        self.assertEqual(self.stored(), ([], []))

    def test_old_operation_is_recoverable_after_24_following_operations(self):
        browser = self.login()
        pack = self.pack(browser)
        key = 'acceptance:' + secrets.token_hex(16)
        self.assertEqual(self.save(browser, pack, key)[0], 201)
        for _ in range(24):
            self.assertEqual(self.save(browser, pack)[0], 200)
        before = self.stored()
        self.assertEqual(len(before[1]), 25)
        self.assertTrue(all(len(row['metadata']['materialization_receipts']) == 20 for row in before[0]))
        status, receipt, _ = self.save(browser, pack, key)
        self.assertEqual(status, 200)
        self.assertTrue(receipt['idempotent_replay'])
        self.assertEqual(self.stored(), before)

    def test_deleted_draft_invalidates_old_receipt_without_recreating_silently(self):
        from database import db
        from models import MessageTemplateRegistry
        browser = self.login()
        pack = self.pack(browser)
        key = 'acceptance:' + secrets.token_hex(16)
        self.assertEqual(self.save(browser, pack, key)[0], 201)
        rows, _ = self.stored()
        with self.app.app_context():
            db.session.delete(db.session.get(MessageTemplateRegistry, rows[0]['id']))
            db.session.commit()
        before = self.stored()
        self.assertEqual(self.save(browser, pack, key)[0], 409)
        self.assertEqual(self.stored(), before)

    def test_writer_fence_blocks_even_a_previously_authorized_session(self):
        browser = self.login()
        pack = self.pack(browser)
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        try:
            self.assertEqual(self.save(browser, pack)[0], 503)
            self.assertEqual(self.stored(), ([], []))
        finally:
            self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False


if __name__ == '__main__':
    unittest.main(verbosity=2)
