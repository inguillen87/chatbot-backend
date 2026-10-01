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


def jpeg_fixture():
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), color='white').save(stream, format='JPEG')
    return stream.getvalue()


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

    def seed(self, client, data=PDF, title='Proyecto de prueba pendiente de aprobación', *, format_name=None, metadata=None):
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['sources']['a']['sha256'] = hashlib.sha256(data).hexdigest()
        bundle['sources']['a']['title'] = title
        if format_name is not None:
            bundle['sources']['a'].update(format=format_name, byte_size=len(data))
            if format_name != 'pdf':
                bundle['sources']['a']['page_count'] = 1
                for refs in bundle['node_evidence'].values():
                    for ref in refs:
                        ref['page'] = 1; ref['pages'] = [1]
        if metadata:
            bundle['sources']['a'].update(metadata)
        response = self.put(client, 'import', None, bundle)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def source_url(self, state, *, public=False, source_id='a'):
        return self.url(public) + '/sources/' + source_id + '?revision=' + state['revision']

    def test_jpeg_and_utf8_text_are_hash_bound_and_safe_private_originals(self):
        client = self.login()
        revision = None
        for format_name, mime, extension, data in [('jpeg', 'image/jpeg', 'jpg', jpeg_fixture()),
                                                  ('text', 'text/plain', 'txt', 'Texto extraído.\nDocumento revisado, sin aprobación.\n'.encode())]:
            bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
            bundle['sources']['a'].update(format=format_name, byte_size=len(data), page_count=1,
                                         sha256=hashlib.sha256(data).hexdigest(), source_authority='operational_document',
                                         origin_url='https://drive.google.com/file/d/source-fixture/view',
                                         review_status='needs_review', current_validity='not_verified')
            for refs in bundle['node_evidence'].values():
                for ref in refs:
                    ref['pages'] = [1]; ref['page'] = 1
            response = self.put(client, 'import', revision, bundle)
            self.assertEqual(response.status_code, 200, response.get_json())
            state = response.get_json(); revision = state['revision']
            source = state['knowledge']['sources'][0]
            self.assertIsNone(source['url'])
            self.assertEqual(source['review_status'], 'needs_review')
            self.assertEqual(source['current_validity'], 'not_verified')
            self.assertEqual(source['pagination'], 'logical_snapshot')
            self.assertNotIn('available', source['delivery'])
            with self.storage(data=data, content_type=mime, extension=extension, sha256=source['sha256']):
                original = client.get(self.source_url(state))
            self.assertEqual(original.status_code, 200, original.get_json())
            self.assertEqual(original.data, data)
            self.assertEqual(original.headers['Content-Type'], mime + ('; charset=utf-8' if format_name == 'text' else ''))
            self.assertEqual(original.headers['Content-Length'], str(len(data)))
            self.assertEqual(original.headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn('attachment' if format_name == 'text' else 'inline', original.headers['Content-Disposition'])
            self.assertNotIn('drive.google.com', str(original.headers))
            answer = client.get(self.url() + '/nodes/requirements?revision=' + revision).get_json()
            cited = answer['nodes'][0]['sources'][0]
            self.assertEqual(cited['delivery'], source['delivery'])
            self.assertEqual(cited['origin_url'], source['origin_url'])

    def test_non_pdf_sources_retain_private_foreign_stale_and_retirement_fences(self):
        client = self.login()
        text = b'Text snapshot\n'
        state = self.seed(client, text, format_name='text')
        path = self.source_url(state)
        with patch('services.institutional_assistant_sources.r2_service.read_object_bytes', side_effect=AssertionError('denied requests must not fetch')):
            self.assertEqual(self.login('acceptance-b').get(path).status_code, 403)
            self.assertIn(self.app.test_client().get(path).status_code, (401, 403))
            self.assertEqual(client.get(path.replace(state['revision'], '0' * 64)).status_code, 412)
            self.assertEqual(self.app.test_client().get(self.source_url(state, public=True)).status_code, 404)
        published = self.put(client, 'publish', state['revision']).get_json()
        with self.storage(data=text, content_type='text/plain', extension='txt', sha256=hashlib.sha256(text).hexdigest()):
            response = self.app.test_client().get(self.source_url(published, public=True))
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertEqual(self.put(client, 'retire', published['revision']).status_code, 200)
        with patch('services.institutional_assistant_sources.r2_service.read_object_bytes', side_effect=AssertionError('retired object must not fetch')):
            self.assertEqual(self.app.test_client().get(self.source_url(published, public=True)).status_code, 404)

    def test_public_workspace_nodes_and_answers_hide_private_source_origin(self):
        origin = 'https://drive.google.com/file/d/private-source-fixture/view'
        official = 'https://example.org/official-source.pdf'
        client = self.login()
        state = self.seed(client, metadata={'origin_url': origin, 'official_url': official,
                                           'source_authority': 'operational_document',
                                           'review_status': 'needs_review', 'evaluation_only': True})
        private_node = client.get(self.url() + '/nodes/requirements?revision=' + state['revision'])
        self.assertEqual(private_node.status_code, 200)
        for source in (state['knowledge']['sources'][0], state['knowledge']['initial']['sources'][0],
                       private_node.get_json()['nodes'][0]['sources'][0]):
            self.assertEqual(source['origin_url'], origin)

        published = self.put(client, 'publish', state['revision']).get_json()
        public = self.app.test_client()
        workspace_response = public.get(self.url(True))
        canonical_response = public.get(self.url(True) + '/nodes/requirements?revision=' + published['revision'])
        with patch('services.llm_utils.llamar_llm_para_json_estructurado', return_value={'node_ids': ['requirements']}):
            answer_response = public.post(self.url(True) + '/answer',
                                          json={'revision': published['revision'], 'question': 'Consulta de prueba'})
        for response in (workspace_response, canonical_response, answer_response):
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertNotIn(origin, response.get_data(as_text=True))
            self.assertNotIn('origin_url', response.get_data(as_text=True))
        sources = workspace_response.get_json()['knowledge']['sources']
        sources += workspace_response.get_json()['knowledge']['initial']['sources']
        sources += canonical_response.get_json()['nodes'][0]['sources']
        sources += answer_response.get_json()['nodes'][0]['sources']
        for source in sources:
            self.assertEqual(source['url'], official)
            self.assertEqual(source['review_status'], 'needs_review')
            self.assertTrue(source['evaluation_only'])

    def test_non_pdf_invalid_encoding_image_signature_size_and_mime_fail_closed(self):
        client = self.login()
        revision = None
        for format_name, mime, extension, data, returned_mime, declared_extra in [
            ('text', 'text/plain', 'txt', b'\xff\xfeinvalid', 'text/plain', 0),
            ('text', 'text/plain', 'txt', b'body\x00hidden', 'text/plain', 0),
            ('text', 'text/plain', 'txt', b'visible text', 'text/html', 0),
            ('text', 'text/plain', 'txt', b'visible text', 'text/plain', 1),
            ('jpeg', 'image/jpeg', 'jpg', b'\xff\xd8\xffnot-an-image\xff\xd9', 'image/jpeg', 0),
            ('jpeg', 'image/jpeg', 'jpg', jpeg_fixture(), 'image/png', 0),
        ]:
            bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
            bundle['sources']['a'].update(format=format_name, byte_size=len(data) + declared_extra,
                                         page_count=1, sha256=hashlib.sha256(data).hexdigest())
            for refs in bundle['node_evidence'].values():
                for ref in refs:
                    ref['pages'] = [1]; ref['page'] = 1
            state_response = self.put(client, 'import', revision, bundle)
            self.assertEqual(state_response.status_code, 200, state_response.get_json())
            state = state_response.get_json(); revision = state['revision']
            with self.subTest(format=format_name, data_size=len(data), type=returned_mime), self.storage(
                    data=data, content_type=returned_mime, extension=extension, sha256=hashlib.sha256(data).hexdigest()):
                response = client.get(self.source_url(state))
                self.assertEqual(response.status_code, 503, response.get_json())
                self.assertEqual(response.get_json()['reason_code'], 'knowledge_source_integrity_failed')
                self.assertNotEqual(response.data, data)

    def test_safe_text_attachment_does_not_interpret_active_markup(self):
        client = self.login(); data = b'<script>fixtureOnly()</script>\n'
        state = self.seed(client, data, title='../../Documento <script>', format_name='text')
        with self.storage(data=data, content_type='text/plain', extension='txt', sha256=hashlib.sha256(data).hexdigest()):
            response = client.get(self.source_url(state))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, data)
        self.assertEqual(response.headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment; filename="Documento_script.txt"', response.headers['Content-Disposition'])
        self.assertNotIn('../', response.headers['Content-Disposition'])
        invalid = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        invalid['sources']['a']['title'] = 'Doc\r\nX-Injected: yes'
        self.assertEqual(self.put(client, 'import', state['revision'], invalid).status_code, 422)

    def test_text_hash_change_and_revision_retirement_during_io_release_no_bytes(self):
        from database import db
        from models import TenantConfig
        from services.institutional_assistant_content import digest
        client = self.login(); data = b'Original text\n'
        state = self.seed(client, data, format_name='text')
        sha = hashlib.sha256(data).hexdigest()
        with self.storage(data=b'Modified text\n', content_type='text/plain', extension='txt', sha256=sha):
            response = client.get(self.source_url(state))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()['reason_code'], 'knowledge_source_integrity_failed')

        def change_revision():
            row = TenantConfig.query.filter_by(tenant_id=self.accounts['acceptance-a']['tenant_id'], key='institutional_assistant').one()
            value = dict(row.json_value); value['generation'] += 1
            value['revision'] = digest({key: value[key] for key in ('bundle_hash', 'generation', 'visibility')})
            row.json_value = value; db.session.commit()

        with self.storage(data=data, content_type='text/plain', extension='txt', sha256=sha, on_read=change_revision):
            response = client.get(self.source_url(state))
            self.assertEqual(response.status_code, 412)
            self.assertEqual(response.get_json()['reason_code'], 'knowledge_revision_conflict')
            self.assertNotEqual(response.data, data)

    @contextmanager
    def storage(self, *, data=PDF, declared_size=None, content_type='application/pdf',
                error=None, on_read=None, sha256=None, extension='pdf'):
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
               + '/sources/' + (sha256 or hashlib.sha256(PDF).hexdigest()) + '.' + extension)
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

    def test_actor_disabled_during_storage_read_releases_no_original_bytes(self):
        from database import db
        from models import User
        from utils.auth_helpers import auth_session_version
        client = self.login(); state = self.seed(client)
        actor_id = self.accounts['acceptance-a']['id']

        def disable_actor():
            actor = db.session.get(User, actor_id)
            before = auth_session_version(actor)
            metadata = dict(actor.accesibilidad or {})
            auth = dict(metadata.get('auth') or {})
            auth['disabled'] = True
            metadata['auth'] = auth
            actor.accesibilidad = metadata
            db.session.commit()
            self.assertEqual(auth_session_version(actor), before)

        try:
            with self.storage(on_read=disable_actor):
                response = client.get(self.source_url(state))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json(), {'reason_code': 'knowledge_forbidden'})
                self.assertNotEqual(response.data, PDF)
        finally:
            with self.app.app_context():
                actor = db.session.get(User, actor_id)
                metadata = dict(actor.accesibilidad or {})
                auth = dict(metadata.get('auth') or {})
                auth.pop('disabled', None)
                metadata['auth'] = auth
                actor.accesibilidad = metadata
                db.session.commit()

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
