"""Local HTTP authorization regression with the real SDK and stubbed storage.

Customer/provider acceptance still requires the deployed route and real PDFs.
"""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__': prepare_process()

from contextlib import contextmanager
import hashlib
import io
import secrets
import unittest
from unittest.mock import patch

from tests import institutional_assistant_http as knowledge_http
from tests.test_institutional_assistant_content import sample


PDF = b'%PDF-1.4\n% Local regression source\n1 0 obj <<>> endobj\n%%EOF\n'
PREFIX = 'knowledge-private/' + 'c' * 32


class KnowledgeSourceHTTPTests(unittest.TestCase):
    setUpClass = classmethod(knowledge_http.KnowledgeHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(knowledge_http.KnowledgeHTTPTests.tearDownClass.__func__)
    login = knowledge_http.KnowledgeHTTPTests.login
    url = knowledge_http.KnowledgeHTTPTests.url
    put = knowledge_http.KnowledgeHTTPTests.put

    def setUp(self):
        knowledge_http.KnowledgeHTTPTests.setUp(self)
        self.app.config['INSTITUTIONAL_KNOWLEDGE_R2_PREFIX'] = PREFIX
        # Each case has an independent disposable rate-limit window.
        from extensions import limiter
        with self.app.app_context():
            limiter.reset()

    def seed(self, client, data=PDF, title='Proyecto de prueba pendiente de aprobación'):
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['sources']['a']['sha256'] = hashlib.sha256(data).hexdigest()
        bundle['sources']['a']['title'] = title
        response = self.put(client, 'import', None, bundle)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def source_url(self, state, *, public=False, source_id='a'):
        return self.url(public) + '/sources/' + source_id + '?revision=' + state['revision']

    @contextmanager
    def storage(self, *, data=PDF, declared_size=None, content_type='application/pdf',
                error=None, on_read=None, sha256=None):
        import boto3
        from botocore.response import StreamingBody
        from botocore.stub import Stubber
        from services.r2_service import R2Service

        class Input(io.BytesIO):
            def read(self, size=-1):
                result = super().read(size)
                if on_read is not None:
                    on_read()
                return result

        service = R2Service()
        service.endpoint_url = 'https://storage.example.invalid'
        service.access_key_id = secrets.token_hex(12)
        service.secret_access_key = secrets.token_hex(24)
        service.bucket_name = 'private-regression-bucket'
        # A public asset URL exists, and must never be a private fallback.
        service.public_base_url = 'https://public.example.invalid'
        client = boto3.client('s3', endpoint_url=service.endpoint_url,
                              aws_access_key_id=service.access_key_id,
                              aws_secret_access_key=service.secret_access_key,
                              region_name='auto')
        stubber = Stubber(client)
        key = (PREFIX + '/tenants/' + str(self.accounts['acceptance-a']['tenant_id'])
               + '/sources/' + (sha256 or hashlib.sha256(PDF).hexdigest()) + '.pdf')
        params = {'Bucket': service.bucket_name, 'Key': key}
        if error:
            stubber.add_client_error('get_object', service_error_code=error,
                                     service_message='provider details must not escape',
                                     http_status_code=404 if error == 'NoSuchKey' else 403,
                                     expected_params=params)
        else:
            stubber.add_response('get_object', {
                'Body': StreamingBody(Input(data), len(data)),
                'ContentLength': len(data) if declared_size is None else declared_size,
                'ContentType': content_type,
            }, params)
        with stubber, patch.object(service, '_create_client', return_value=client) as create, \
             patch('services.institutional_assistant_sources.r2_service', service):
            yield service, create
            stubber.assert_no_pending_responses()

    def test_private_pdf_uses_exact_tenant_source_hash_and_never_returns_object_urls(self):
        from database import db
        client = self.login(); state = self.seed(client)
        with self.storage() as (_, create), patch.object(db.session, 'commit', side_effect=AssertionError('read must not commit')):
            response = client.get(self.source_url(state))
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.data, PDF)
        create.assert_called_once_with(timeout_seconds=5)
        self.assertEqual(response.headers['Content-Type'], 'application/pdf')
        self.assertEqual(int(response.headers['Content-Length']), len(PDF))
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(response.headers['Vary'], 'Cookie, Authorization, Origin')
        self.assertIn('inline; filename="Proyecto_de_prueba_pendiente_de_aprobacion.pdf"', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertNotIn('private-regression-bucket', str(response.headers))
        self.assertNotIn(PREFIX, str(response.headers))
        self.assertNotIn('public.example.invalid', str(response.headers))

    def test_private_acl_and_revision_reject_before_storage(self):
        client = self.login(); state = self.seed(client); path = self.source_url(state)
        with patch('services.institutional_assistant_sources.r2_service.read_object_bytes', side_effect=AssertionError('denied requests must not fetch objects')):
            for account in ('acceptance-b', 'viewer'):
                self.assertEqual(self.login(account).get(path).status_code, 403)
            self.assertIn(self.app.test_client().get(path).status_code, (401, 403))
            self.assertEqual(client.get(path, headers={'Origin': 'https://foreign.example.invalid'}).status_code, 403)
            self.assertEqual(client.get(path, headers={'X-Tenant': 'acceptance-b'}).status_code, 400)
            self.assertEqual(client.get(path.replace(state['revision'], '0' * 64)).status_code, 412)
            self.assertEqual(client.get(self.source_url(state, source_id='missing')).status_code, 404)
            for suffix in ('&revision=' + state['revision'], '&url=https://public.example.invalid/a.pdf', '&key=foreign', '&question=x'):
                self.assertEqual(client.get(path + suffix).status_code, 400)
            self.assertEqual(client.get(path.split('?')[0]).status_code, 400)
            self.assertEqual(client.get(path, json={'source_id': 'a'}).status_code, 400)

    def test_public_source_is_private_until_published_and_retirement_revokes_old_link(self):
        client = self.login(); private = self.seed(client); public = self.app.test_client()
        with patch('services.institutional_assistant_sources.r2_service.read_object_bytes', side_effect=AssertionError('private source must not fetch')):
            self.assertEqual(public.get(self.source_url(private, public=True)).status_code, 404)
        published = self.put(client, 'publish', private['revision']).get_json()
        with self.storage():
            response = public.get(self.source_url(published, public=True), headers={'Origin': 'https://institution.example.invalid'})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.data, PDF)
        self.assertEqual(public.get(self.source_url(private, public=True)).status_code, 412)
        self.assertEqual(self.put(client, 'retire', published['revision']).status_code, 200)
        with patch('services.institutional_assistant_sources.r2_service.read_object_bytes', side_effect=AssertionError('retired source must not fetch')):
            self.assertEqual(public.get(self.source_url(published, public=True)).status_code, 404)

    def test_storage_corruption_type_size_and_non_pdf_are_fail_closed(self):
        client = self.login(); state = self.seed(client)
        for options in ({'data': PDF + b'corrupted'}, {'content_type': 'text/html'},
                        {'declared_size': 8 * 1024 * 1024 + 1}, {'declared_size': len(PDF) - 1}):
            with self.subTest(options=options), self.storage(**options):
                response = client.get(self.source_url(state))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json(), {'reason_code': 'knowledge_source_integrity_failed'})
                self.assertNotIn('provider', response.get_data(as_text=True))
        non_pdf = b'<html>not a PDF</html>'
        response = self.put(client, 'import', state['revision'], {
            **sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a'),
            'sources': {'a': {'id': 'a', 'title': 'Fuente', 'sha256': hashlib.sha256(non_pdf).hexdigest(), 'page_count': 1}},
        })
        self.assertEqual(response.status_code, 422)  # evidence references page 2
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['sources']['a']['sha256'] = hashlib.sha256(non_pdf).hexdigest()
        imported = self.put(client, 'import', state['revision'], bundle).get_json()
        with self.storage(data=non_pdf, sha256=hashlib.sha256(non_pdf).hexdigest()):
            denied = client.get(self.source_url(imported))
            self.assertEqual(denied.status_code, 503)
            self.assertEqual(denied.get_json()['reason_code'], 'knowledge_source_integrity_failed')

    def test_missing_storage_has_no_public_fallback_and_provider_errors_are_value_free(self):
        client = self.login(); state = self.seed(client)
        for prefix in ('', 'public/assets', 'knowledge-private/../foreign', None):
            self.app.config['INSTITUTIONAL_KNOWLEDGE_R2_PREFIX'] = prefix
            with patch('services.institutional_assistant_sources.r2_service.read_object_bytes', side_effect=AssertionError('invalid config must not fetch')):
                response = client.get(self.source_url(state))
                self.assertEqual(response.status_code, 503)
        self.app.config['INSTITUTIONAL_KNOWLEDGE_R2_PREFIX'] = PREFIX
        for error, status, reason in (('NoSuchKey', 404, 'knowledge_source_not_available'), ('AccessDenied', 503, 'knowledge_source_storage_unavailable')):
            with self.storage(error=error):
                response = client.get(self.source_url(state))
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.get_json(), {'reason_code': reason})

    def test_retirement_and_session_revocation_during_download_release_no_bytes(self):
        from database import db
        from models import TenantConfig, User
        from services.institutional_assistant_content import digest
        from utils.auth_helpers import bump_auth_session_version
        client = self.login(); state = self.seed(client)

        def revoke_session():
            owner = db.session.get(User, self.accounts['acceptance-a']['id'])
            bump_auth_session_version(owner); db.session.commit()

        with self.storage(on_read=revoke_session):
            response = client.get(self.source_url(state))
            self.assertEqual(response.status_code, 403)
            self.assertNotEqual(response.data, PDF)
        client = self.login()
        published = self.put(client, 'publish', state['revision']).get_json()

        def retire():
            row = TenantConfig.query.filter_by(tenant_id=self.accounts['acceptance-a']['tenant_id'], key='institutional_assistant').one()
            value = dict(row.json_value); value['visibility'] = 'private'; value['generation'] += 1
            value['revision'] = digest({key: value[key] for key in ('bundle_hash', 'generation', 'visibility')})
            row.json_value = value; db.session.commit()

        with self.storage(on_read=retire):
            response = self.app.test_client().get(self.source_url(published, public=True))
            self.assertEqual(response.status_code, 404)
            self.assertNotEqual(response.data, PDF)

    def test_pdf_delivery_real_http_normal_login_cookie_and_tenant_isolation(self):
        import requests
        from threading import Thread
        from werkzeug.serving import make_server
        client = self.login(); state = self.seed(client)
        server = make_server('127.0.0.1', 0, self.app, threaded=True)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        owner = requests.Session(); foreign = requests.Session(); anonymous = requests.Session()
        try:
            for session, account in ((owner, 'acceptance-a'), (foreign, 'acceptance-b')):
                response = session.post(base + '/auth/login', json={'email': self.accounts[account]['email'], 'password': self.password}, timeout=10)
                self.assertEqual(response.status_code, 200)
            with self.storage():
                own = owner.get(base + self.source_url(state), timeout=10)
                self.assertEqual(own.status_code, 200); self.assertEqual(own.content, PDF)
            self.assertEqual(foreign.get(base + self.source_url(state), timeout=10).status_code, 403)
            self.assertIn(anonymous.get(base + self.source_url(state), timeout=10).status_code, (401, 403))
            self.assertEqual(anonymous.get(base + self.source_url(state, public=True), timeout=10).status_code, 404)
            self.assertEqual(owner.get(base + self.source_url(state).replace(state['revision'], '0' * 64), timeout=10).status_code, 412)
        finally:
            owner.close(); foreign.close(); anonymous.close()
            server.shutdown(); thread.join(timeout=5); server.server_close()

    def test_membership_conflict_or_inactive_tenant_during_read_releases_no_pdf(self):
        from database import db
        from models import User, TenantProfile
        for change, expected in (('membership', 403), ('tenant', 404)):
            client = self.login(); state = self.seed(client)

            def revoke():
                if change == 'membership':
                    owner = db.session.get(User, self.accounts['acceptance-a']['id'])
                    owner.tenant_slug = 'acceptance-b'
                else:
                    tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
                    tenant.is_active = False
                db.session.commit()

            try:
                with self.subTest(change=change), self.storage(on_read=revoke):
                    response = client.get(self.source_url(state))
                    self.assertEqual(response.status_code, expected)
                    self.assertNotEqual(response.data, PDF)
            finally:
                with self.app.app_context():
                    owner = db.session.get(User, self.accounts['acceptance-a']['id'])
                    owner.tenant_slug = 'acceptance-a'
                    tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
                    tenant.is_active = True
                    db.session.commit()
            with self.app.app_context():
                from models import TenantConfig
                TenantConfig.query.filter_by(key='institutional_assistant').delete()
                db.session.commit()


if __name__ == '__main__': unittest.main()
