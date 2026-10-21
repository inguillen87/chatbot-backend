"""Disposable HTTP/SDK regression evidence; no provider or customer access.

Run only in its isolated process:
    python -m tests.test_private_attachment_storage_http
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from tests import institutional_assistant_http as knowledge_fixtures


PRIVATE_BUCKET = 'private-attachments-fixture'
GENERAL_BUCKET = 'public-assets-fixture'
PRIVATE_ACCESS = 'private-attachment-fixture-access'
PRIVATE_SECRET = 'private-attachment-fixture-secret'
GENERAL_ACCESS = 'general-attachment-fixture-access'
GENERAL_SECRET = 'general-attachment-fixture-secret'
ENDPOINT = 'https://storage.example.invalid'
PRIVATE_ENDPOINT = 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com'
CDN = 'https://cdn.example.invalid'
PREFIX = 'knowledge-private/' + 'a' * 32
KEY = 'private-attachments/' + 'a' * 32 + '/tenants/acceptance-a/attachments/' + 'b' * 32 + '.pdf'
REFERENCE = 'r2-private://' + PRIVATE_BUCKET + '/' + KEY
DATA = b'Disposable attachment fixture; no citizen data.\n'
SHA256 = hashlib.sha256(DATA).hexdigest()
FILENAME = 'private-attachment-fixture.pdf'
SESSION_ID = 'fixture-attachment-session'
LEGACY_CDN = CDN + '/municipios/acceptance-a/reclamos/legacy.pdf'
LEGACY_EXTERNAL = 'https://storage.googleapis.com/legacy-fixture/scan.pdf'


class PrivateAttachmentHTTPTests(unittest.TestCase):
    """Reuse the disposable app/login fixtures without inheriting their tests."""

    @classmethod
    def setUpClass(cls):
        knowledge_fixtures.KnowledgeHTTPTests.setUpClass.__func__(cls)
        from flask import current_app, g, jsonify, Response
        from utils.auth_helpers import token_requerido

        def raw_payload():
            return jsonify(deepcopy(current_app.config['PRIVATE_ATTACHMENT_TEST_PAYLOAD']))

        @token_requerido
        def authenticated_payload(current_user):
            from database import db
            from models import ArchivoAdjunto
            from services.attachment_delivery import serialize_attachment_for_delivery
            row = db.session.get(ArchivoAdjunto, current_app.config['PRIVATE_ATTACHMENT_TEST_ID'])
            return jsonify({'attachmentInfo': serialize_attachment_for_delivery(
                row, meta={'original': row.url})})

        @token_requerido
        def authenticated_raw_payload(current_user):
            return raw_payload()

        @token_requerido
        def authenticated_dict_payload(current_user):
            from services.attachment_delivery import serialize_attachment_for_delivery
            return jsonify({'attachmentInfo': serialize_attachment_for_delivery(
                deepcopy(current_app.config['PRIVATE_ATTACHMENT_TEST_PAYLOAD']))})

        @token_requerido
        def authenticated_bytes(current_user):
            from database import db
            from models import ArchivoAdjunto
            from services.attachment_delivery import read_authorized_attachment_bytes
            from services.r2_service import R2ObjectStorageUnavailableError
            if current_app.config.get('PRIVATE_ATTACHMENT_TEST_WIDGET'):
                g.widget_session = True
            try:
                data = read_authorized_attachment_bytes(
                    current_app.config['PRIVATE_ATTACHMENT_TEST_READ_REFERENCE'],
                    'application/pdf', max_bytes=current_app.config['PRIVATE_ATTACHMENT_TEST_READ_LIMIT'],
                    attachment=(db.session.get(ArchivoAdjunto, current_app.config['PRIVATE_ATTACHMENT_TEST_ID'])
                                if current_app.config['PRIVATE_ATTACHMENT_TEST_READ_RESOURCE'] else None))
            except R2ObjectStorageUnavailableError:
                return jsonify({'reason_code': 'fixture_attachment_read_denied'}), 503
            return Response(data, mimetype='application/pdf',
                            headers={'Cache-Control': 'private, no-store'})

        cls.app.add_url_rule('/_acceptance/private-attachments/raw',
                             endpoint='attachment_fixture_raw', view_func=raw_payload)
        cls.app.add_url_rule('/_acceptance/private-attachments/authenticated',
                             endpoint='attachment_fixture_authenticated',
                             view_func=authenticated_payload)
        cls.app.add_url_rule('/_acceptance/private-attachments/bytes',
                             endpoint='attachment_fixture_bytes', view_func=authenticated_bytes)
        cls.app.add_url_rule('/_acceptance/private-attachments/authenticated-raw',
                             endpoint='attachment_fixture_authenticated_raw', view_func=authenticated_raw_payload)
        cls.app.add_url_rule('/_acceptance/private-attachments/serialized-dict',
                             endpoint='attachment_fixture_serialized_dict', view_func=authenticated_dict_payload)

    @classmethod
    def tearDownClass(cls):
        knowledge_fixtures.KnowledgeHTTPTests.tearDownClass.__func__(cls)

    def setUp(self):
        knowledge_fixtures.KnowledgeHTTPTests.setUp(self)
        from database import db
        from models import ArchivoAdjunto, AnalisisArchivo, User
        from services.r2_service import r2_service

        self.app.config.update({
            'INSTITUTIONAL_KNOWLEDGE_R2_BUCKET_NAME': PRIVATE_BUCKET,
            'INSTITUTIONAL_KNOWLEDGE_R2_ACCESS_KEY_ID': PRIVATE_ACCESS,
            'INSTITUTIONAL_KNOWLEDGE_R2_SECRET_ACCESS_KEY': PRIVATE_SECRET,
            'INSTITUTIONAL_KNOWLEDGE_R2_PREFIX': PREFIX,
            'INSTITUTIONAL_KNOWLEDGE_R2_ENDPOINT_URL': None,
            'R2_ENDPOINT_URL': ENDPOINT,
            'R2_BUCKET_NAME': GENERAL_BUCKET,
            'R2_ACCESS_KEY_ID': GENERAL_ACCESS,
            'R2_SECRET_ACCESS_KEY': GENERAL_SECRET,
            'PRIVATE_ATTACHMENT_LEGACY_MAP_JSON': None,
            'PRIVATE_ATTACHMENT_TEST_PAYLOAD': {},
            'PRIVATE_ATTACHMENT_TEST_READ_REFERENCE': REFERENCE,
            'PRIVATE_ATTACHMENT_TEST_READ_LIMIT': len(DATA),
            'PRIVATE_ATTACHMENT_TEST_READ_RESOURCE': True,
            'PRIVATE_ATTACHMENT_TEST_WIDGET': False,
        })
        for attribute, value in {
            'endpoint_url': ENDPOINT, 'bucket_name': GENERAL_BUCKET,
            'access_key_id': GENERAL_ACCESS, 'secret_access_key': GENERAL_SECRET,
            'public_base_url': CDN, 'client': None,
            '_client_initialization_attempted': False,
        }.items():
            override = patch.object(r2_service, attribute, value)
            override.start()
            self.addCleanup(override.stop)
        with self.app.app_context():
            AnalisisArchivo.query.delete()
            ArchivoAdjunto.query.delete()
            for account in ('acceptance-a', 'acceptance-b'):
                actor = db.session.get(User, self.accounts[account]['id'])
                actor.accesibilidad = {}
                actor.rol = 'admin'
                actor.tenant_id = self.accounts[account]['tenant_id']
                actor.tenant_slug = account
                actor.municipio_id = actor.id
            row = ArchivoAdjunto(user_id=self.accounts['acceptance-a']['id'],
                                 session_id=SESSION_ID, filename=FILENAME,
                                 nombre_original='Disposable scan.pdf',
                                 mime='application/pdf', tamano=len(DATA),
                                 tipo='chat', url=REFERENCE)
            db.session.add(row)
            db.session.commit()
            self.attachment_id = row.id
            self.app.config['PRIVATE_ATTACHMENT_TEST_ID'] = row.id

    def login(self, key='acceptance-a'):
        return knowledge_fixtures.KnowledgeHTTPTests.login(self, key)

    def attachment_url(self):
        return '/archivos/' + FILENAME

    def replace_reference(self, reference):
        from database import db
        from models import ArchivoAdjunto
        with self.app.app_context():
            db.session.get(ArchivoAdjunto, self.attachment_id).url = reference
            db.session.commit()

    def mapped(self, original=LEGACY_EXTERNAL, **changes):
        record = {'private_ref': REFERENCE, 'verification_receipt_id': 'fixture-verified-copy',
                  'sha256': SHA256, 'byte_size': len(DATA), 'mime_type': 'application/pdf',
                  'tenant_slug': 'acceptance-a', 'private_object_etag': '"fixture-etag"'}
        record.update(changes)
        self.app.config['PRIVATE_ATTACHMENT_LEGACY_MAP_JSON'] = json.dumps({
            'schema': 'private.attachment.mapping.v1',
            'records': {hashlib.sha256(original.encode('utf-8')).hexdigest(): record},
        })

    @contextmanager
    def sdk_head(self, *, metadata=None, on_head=None, count=1):
        """Use real signing and Stubber, replacing only SDK client creation."""
        import boto3
        from botocore.config import Config
        from botocore.stub import Stubber
        from services.private_attachment_storage import PrivateAttachmentStorage

        client = boto3.client('s3', endpoint_url=ENDPOINT,
                              aws_access_key_id=PRIVATE_ACCESS,
                              aws_secret_access_key=PRIVATE_SECRET, region_name='auto',
                              config=Config(signature_version='s3v4'))
        stubber = Stubber(client)
        response = {'ContentLength': len(DATA), 'ContentType': 'application/pdf',
                    'ETag': '"fixture-etag"', 'Metadata': {'sha256': SHA256}}
        if metadata:
            response.update(metadata)
        for _ in range(count):
            stubber.add_response('head_object', response, {'Bucket': PRIVATE_BUCKET, 'Key': KEY})
        if on_head is not None:
            client.meta.events.register('after-call.s3.HeadObject',
                                        lambda **kwargs: on_head())
        try:
            with stubber, patch.object(PrivateAttachmentStorage, '_create_client',
                                       return_value=client) as factory:
                yield client, factory
                stubber.assert_no_pending_responses()
        finally:
            client.close()

    def assert_private_redirect(self, response):
        self.assertEqual(response.status_code, 302, response.get_json())
        location = response.headers['Location']
        parsed = urlsplit(location)
        query = parse_qs(parsed.query)
        self.assertEqual(query['X-Amz-Expires'], ['120'])
        self.assertTrue(query['X-Amz-Credential'][0].startswith(PRIVATE_ACCESS + '/'))
        self.assertEqual(query['response-cache-control'], ['private, no-store'])
        self.assertIn(PRIVATE_BUCKET, parsed.path + '/' + parsed.netloc)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertNotIn('r2-private://', response.get_data(as_text=True))
        self.assertNotIn(GENERAL_ACCESS, location)
        return location

    @contextmanager
    def sdk_read(self, *, data=DATA, declared_size=None, mime='application/pdf',
                 stored_hash=SHA256, etag='"fixture-etag"', on_read=None):
        import boto3
        from botocore.response import StreamingBody
        from botocore.stub import Stubber
        from services.private_attachment_storage import PrivateAttachmentStorage

        class Input(io.BytesIO):
            def __init__(self, value):
                super().__init__(value)
                self.requested = []
            def read(self, size=-1):
                self.requested.append(size)
                result = super().read(size)
                if on_read is not None:
                    on_read()
                return result

        stream = Input(data)
        client = boto3.client('s3', endpoint_url=ENDPOINT, region_name='auto',
                              aws_access_key_id=PRIVATE_ACCESS, aws_secret_access_key=PRIVATE_SECRET)
        stubber = Stubber(client)
        stubber.add_response('get_object', {
            'Body': StreamingBody(stream, len(data)),
            'ContentLength': len(data) if declared_size is None else declared_size,
            'ContentType': mime, 'Metadata': {'sha256': stored_hash}, 'ETag': etag,
        }, {'Bucket': PRIVATE_BUCKET, 'Key': KEY})
        try:
            with stubber, patch.object(PrivateAttachmentStorage, '_create_client', return_value=client):
                yield stream
                stubber.assert_no_pending_responses()
            self.assertTrue(stream.closed)
        finally:
            client.close()

    def test_backend_reader_returns_only_verified_bounded_private_bytes(self):
        client = self.login()
        with self.sdk_read() as stream:
            response = client.get('/_acceptance/private-attachments/bytes')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.data, DATA)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(stream.requested, [len(DATA) + 1])
        self.assertNotIn('r2-private://', str(response.headers))

    def test_backend_reader_retirement_during_read_releases_no_bytes(self):
        from services.auth_session_lifecycle import retire_session
        client = self.login()
        descriptor = client.get('/auth/me').get_json()['session_retirement']
        receipts = []
        def retire():
            receipt = retire_session(descriptor['proof'], uuid4().hex)
            self.assertTrue(receipt['local_revoked'])
            receipts.append(receipt)
        with self.sdk_read(on_read=retire):
            response = client.get('/_acceptance/private-attachments/bytes')
        self.assertEqual(len(receipts), 1)
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertNotIn(DATA, response.data)
        self.assert_no_private_delivery(response)

    def test_backend_reader_rejects_integrity_size_mime_and_borrowed_widget_context(self):
        from services.private_attachment_storage import PrivateAttachmentStorage
        client = self.login()
        self.mapped()
        self.replace_reference(LEGACY_EXTERNAL)
        self.app.config['PRIVATE_ATTACHMENT_TEST_READ_REFERENCE'] = LEGACY_EXTERNAL
        for options in ({'declared_size': len(DATA) + 1}, {'mime': 'text/plain'},
                        {'stored_hash': 'd' * 64}, {'etag': '"different-etag"'}, {'data': b'x' * len(DATA),
                                                 'stored_hash': hashlib.sha256(b'x' * len(DATA)).hexdigest()}):
            with self.subTest(options=options), self.sdk_read(**options):
                response = client.get('/_acceptance/private-attachments/bytes')
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assert_no_private_delivery(response)
            self.assertNotIn(DATA, response.data)
        self.app.config['PRIVATE_ATTACHMENT_TEST_WIDGET'] = True
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('widget cannot borrow native private reader')) as factory:
            response = client.get('/_acceptance/private-attachments/bytes')
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assert_no_private_delivery(response)
            factory.assert_not_called()
        self.app.config['PRIVATE_ATTACHMENT_TEST_WIDGET'] = False
        self.app.config['PRIVATE_ATTACHMENT_TEST_READ_RESOURCE'] = False
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('reference without exact resource cannot reach SDK')) as factory:
            response = client.get('/_acceptance/private-attachments/bytes')
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assert_no_private_delivery(response)
            factory.assert_not_called()

    def assert_no_private_delivery(self, response):
        self.assertNotIn('Location', response.headers)
        serialized = response.get_data(as_text=True)
        self.assertNotIn('r2-private://', serialized)
        self.assertNotIn('X-Amz-', serialized)
        self.assertNotIn('"storage_url"', serialized)

    def test_native_authenticated_download_uses_private_sdk_and_short_redirect(self):
        client = self.login()
        with self.sdk_head() as (_, factory):
            response = client.get(self.attachment_url())
        self.assert_private_redirect(response)
        self.assertTrue(factory.called)

    def test_foreign_anonymous_and_wrong_cookie_sid_do_not_touch_sdk(self):
        from database import db
        from models import AuthSession
        from services.private_attachment_storage import PrivateAttachmentStorage
        other = self.login('acceptance-b')
        own = self.login()
        descriptor = own.get('/auth/me').get_json()['session_retirement']
        own.delete_cookie('auth_token')
        with self.app.app_context():
            row = db.session.get(AuthSession, descriptor['lineage_id'])
            row.flask_sid_hash = hashlib.sha256(b'different-native-session-id').hexdigest()
            db.session.commit()
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('denied identity cannot reach SDK')):
            for client in (other, self.app.test_client(), own):
                with self.subTest(identity=client):
                    response = client.get(self.attachment_url())
                    self.assertIn(response.status_code, (401, 403))
                    self.assert_no_private_delivery(response)

    def test_exact_native_retirement_during_head_releases_no_signed_url(self):
        from services.auth_session_lifecycle import retire_session
        client = self.login()
        descriptor = client.get('/auth/me').get_json()['session_retirement']
        receipts = []

        def retire():
            receipt = retire_session(descriptor['proof'], uuid4().hex)
            self.assertTrue(receipt['local_revoked'])
            receipts.append(receipt)

        with self.sdk_head(on_head=retire):
            response = client.get(self.attachment_url())
        self.assertEqual(len(receipts), 1)
        self.assertIn(response.status_code, (401, 403, 503))
        self.assert_no_private_delivery(response)

    def test_durable_clerk_credential_requires_exact_provider_sid(self):
        from database import db
        from models import AuthSession, User
        from services.auth_session_lifecycle import issue_token, descriptor_for_token
        from services.private_attachment_storage import PrivateAttachmentStorage
        from utils.auth_helpers import auth_session_version
        with self.app.app_context():
            actor = db.session.get(User, self.accounts['acceptance-a']['id'])
            actor.accesibilidad = {'auth': {'provider': 'clerk', 'session_version': 1,
                'clerk': {'user_id': 'synthetic_attachment_clerk_actor'}}}
            db.session.commit()
            token = issue_token({'user_id': actor.id, 'rol': actor.rol,
                'auth_provider': 'clerk', 'session_kind': 'clerk',
                'sid': 'synthetic_attachment_sid_A', 'clerk_sid': 'synthetic_attachment_sid_A',
                'sv': auth_session_version(actor),
                'exp': datetime.now(timezone.utc) + timedelta(hours=1)})
            descriptor = descriptor_for_token(token)
        headers = {'Authorization': 'Bearer ' + token}
        client = self.app.test_client()
        try:
            with self.sdk_head():
                response = client.get(self.attachment_url(), headers=headers)
            self.assert_private_redirect(response)
            with self.app.app_context():
                db.session.get(AuthSession, descriptor['lineage_id']).provider_session_id = 'synthetic_attachment_sid_B'
                db.session.commit()
            with patch.object(PrivateAttachmentStorage, '_create_client',
                              side_effect=AssertionError('different Clerk SID cannot reach SDK')):
                response = client.get(self.attachment_url(), headers=headers)
            self.assertIn(response.status_code, (401, 403))
            self.assert_no_private_delivery(response)
        finally:
            with self.app.app_context():
                db.session.get(User, self.accounts['acceptance-a']['id']).accesibilidad = {}
                db.session.commit()

    def test_disabled_actor_or_tenant_during_head_releases_no_signed_url(self):
        from database import db
        from models import TenantProfile, User
        for target in ('actor', 'tenant'):
            with self.subTest(target=target):
                client = self.login()

                def disable():
                    if target == 'actor':
                        actor = db.session.get(User, self.accounts['acceptance-a']['id'])
                        actor.accesibilidad = {'auth': {'disabled': True}}
                    else:
                        tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
                        tenant.is_active = False
                    db.session.commit()

                try:
                    with self.sdk_head(on_head=disable):
                        response = client.get(self.attachment_url())
                    self.assertIn(response.status_code, (401, 403, 503))
                    self.assert_no_private_delivery(response)
                finally:
                    with self.app.app_context():
                        db.session.get(User, self.accounts['acceptance-a']['id']).accesibilidad = {}
                        db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id']).is_active = True
                        db.session.commit()

    def test_same_tenant_wrong_owner_and_foreign_ticket_are_denied_without_sdk(self):
        from database import db
        from models import ArchivoAdjunto, MunicipioTicket
        from services.private_attachment_storage import PrivateAttachmentStorage
        second = self.login('second')
        owner = self.login()
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('resource ACL denial cannot reach SDK')) as factory:
            response = second.get(self.attachment_url())
            self.assertEqual(response.status_code, 403, response.get_json())
            self.assert_no_private_delivery(response)
            response = second.get('/_acceptance/private-attachments/bytes')
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assert_no_private_delivery(response)
            with self.app.app_context():
                ticket = MunicipioTicket(tenant_id=self.accounts['acceptance-b']['tenant_id'],
                    municipio_id=self.accounts['acceptance-b']['id'], pregunta='Disposable ticket fixture')
                db.session.add(ticket)
                db.session.flush()
                db.session.get(ArchivoAdjunto, self.attachment_id).municipio_ticket_id = ticket.id
                db.session.commit()
            response = owner.get(self.attachment_url())
            self.assertEqual(response.status_code, 404, response.get_json())
            self.assert_no_private_delivery(response)
            factory.assert_not_called()

    def test_forged_attachment_dictionary_cannot_sign_reference_without_exact_resource(self):
        from database import db
        from models import ArchivoAdjunto
        from services.private_attachment_storage import PrivateAttachmentStorage
        client = self.login()
        with self.app.app_context():
            other = ArchivoAdjunto(user_id=self.accounts['acceptance-b']['id'],
                filename='foreign-resource-fixture.pdf', mime='application/pdf', tamano=len(DATA),
                tipo='chat', url=REFERENCE)
            db.session.add(other)
            db.session.commit()
            other_id = other.id
        cases = [
            {'url': REFERENCE},
            {'id': 999999, 'url': REFERENCE},
            {'id': self.attachment_id, 'url': REFERENCE.replace('b' * 32, 'c' * 32)},
            {'id': other_id, 'url': REFERENCE},
        ]
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('forged resource cannot reach SDK')) as factory:
            for case in cases:
                with self.subTest(case=case):
                    self.app.config['PRIVATE_ATTACHMENT_TEST_PAYLOAD'] = {
                        'filename': FILENAME, 'mimeType': 'application/pdf', **case}
                    response = client.get('/_acceptance/private-attachments/serialized-dict')
                    self.assertEqual(response.status_code, 200, response.get_json())
                    payload = response.get_json()['attachmentInfo']
                    self.assertIsNone(payload['url'])
                    self.assertFalse(payload['is_private'])
                    self.assertEqual(payload['storage_access'], 'unavailable')
                    self.assert_no_private_delivery(response)
            factory.assert_not_called()

    def test_resource_acl_revoked_during_head_releases_no_signed_url(self):
        from database import db
        from models import ArchivoAdjunto, MunicipioTicket, User
        with self.app.app_context():
            ticket = MunicipioTicket(tenant_id=self.accounts['acceptance-b']['tenant_id'],
                municipio_id=self.accounts['acceptance-b']['id'], pregunta='Disposable reassociation fixture')
            db.session.add(ticket)
            db.session.commit()
            foreign_ticket_id = ticket.id
        for target in ('role', 'owner', 'ticket'):
            with self.subTest(target=target):
                client = self.login()
                def revoke_resource():
                    row = db.session.get(ArchivoAdjunto, self.attachment_id)
                    if target == 'role':
                        db.session.get(User, self.accounts['acceptance-a']['id']).rol = 'ciudadano'
                    elif target == 'owner':
                        row.user_id = self.accounts['second']['id']
                    else:
                        row.municipio_ticket_id = foreign_ticket_id
                    db.session.commit()
                try:
                    with self.sdk_head(on_head=revoke_resource):
                        response = client.get(self.attachment_url())
                    self.assertEqual(response.status_code, 503, response.get_json())
                    self.assert_no_private_delivery(response)
                finally:
                    with self.app.app_context():
                        row = db.session.get(ArchivoAdjunto, self.attachment_id)
                        row.user_id = self.accounts['acceptance-a']['id']
                        row.municipio_ticket_id = None
                        db.session.get(User, self.accounts['acceptance-a']['id']).rol = 'admin'
                        db.session.commit()

    def test_sdk_factory_uses_private_pair_without_modifying_general_service(self):
        from services.private_attachment_storage import private_attachment_storage
        from services.r2_service import r2_service
        with self.app.app_context():
            storage = private_attachment_storage()
            with patch('boto3.client', return_value=object()) as factory:
                storage._create_client(timeout_seconds=30)
            kwargs = factory.call_args.kwargs
            self.assertEqual(kwargs['aws_access_key_id'], PRIVATE_ACCESS)
            self.assertEqual(kwargs['aws_secret_access_key'], PRIVATE_SECRET)
            self.assertEqual(kwargs['endpoint_url'], ENDPOINT)
            self.assertGreater(kwargs['config'].connect_timeout, 0)
            self.assertLessEqual(kwargs['config'].connect_timeout, 5)
            self.assertGreater(kwargs['config'].read_timeout, 0)
            self.assertLessEqual(kwargs['config'].read_timeout, 5)
            self.assertEqual(kwargs['config'].retries['total_max_attempts'], 1)
            self.assertEqual(r2_service.access_key_id, GENERAL_ACCESS)
            self.assertEqual(r2_service.secret_access_key, GENERAL_SECRET)
            self.assertEqual(r2_service.bucket_name, GENERAL_BUCKET)
            self.assertIsNone(storage.public_base_url)

    def test_optional_private_endpoint_is_preferred_and_invalid_value_never_falls_back(self):
        from services.private_attachment_storage import private_attachment_storage
        from services.r2_service import R2ObjectStorageUnavailableError
        with self.app.app_context():
            self.assertEqual(private_attachment_storage().endpoint_url, ENDPOINT)
            with patch.dict(self.app.config, {'INSTITUTIONAL_KNOWLEDGE_R2_ENDPOINT_URL': PRIVATE_ENDPOINT}):
                storage = private_attachment_storage()
                with patch('boto3.client', return_value=object()) as factory:
                    storage._create_client()
                self.assertEqual(factory.call_args.kwargs['endpoint_url'], PRIVATE_ENDPOINT)
                self.assertEqual(factory.call_args.kwargs['aws_access_key_id'], PRIVATE_ACCESS)
            invalid = ('', ENDPOINT, PRIVATE_ENDPOINT.replace('https:', 'http:'),
                PRIVATE_ENDPOINT + '/unexpected-path', PRIVATE_ENDPOINT + '?secret=fixture',
                PRIVATE_ENDPOINT.replace('https://', 'https://user:pass@'),
                PRIVATE_ENDPOINT.replace('a' * 32, 'short-account'), PRIVATE_ENDPOINT + ':443')
            with patch('boto3.client', side_effect=AssertionError('invalid endpoint cannot create SDK')) as factory:
                for endpoint in invalid:
                    with self.subTest(endpoint=endpoint), patch.dict(self.app.config,
                            {'INSTITUTIONAL_KNOWLEDGE_R2_ENDPOINT_URL': endpoint}):
                        with self.assertRaises(R2ObjectStorageUnavailableError):
                            private_attachment_storage()
                factory.assert_not_called()

    def test_missing_or_shared_private_settings_never_fall_back(self):
        from services.private_attachment_storage import private_attachment_storage
        from services.r2_service import R2ObjectStorageUnavailableError
        cases = [
            ('INSTITUTIONAL_KNOWLEDGE_R2_ACCESS_KEY_ID', None),
            ('INSTITUTIONAL_KNOWLEDGE_R2_SECRET_ACCESS_KEY', ''),
            ('INSTITUTIONAL_KNOWLEDGE_R2_ACCESS_KEY_ID', GENERAL_ACCESS),
            ('INSTITUTIONAL_KNOWLEDGE_R2_BUCKET_NAME', GENERAL_BUCKET),
            ('INSTITUTIONAL_KNOWLEDGE_R2_PREFIX', None),
            ('INSTITUTIONAL_KNOWLEDGE_R2_PREFIX', 'knowledge-private/not-an-opaque-prefix'),
        ]
        with self.app.app_context(), patch('boto3.client',
                                          side_effect=AssertionError('invalid settings cannot create SDK')):
            for name, value in cases:
                with self.subTest(setting=name, value=value), patch.dict(self.app.config, {name: value}):
                    with self.assertRaises(R2ObjectStorageUnavailableError):
                        private_attachment_storage()

    def test_private_upload_puts_exact_private_key_size_hash_and_cache_metadata(self):
        import boto3
        from botocore.stub import Stubber
        from services.private_attachment_storage import PrivateAttachmentStorage, private_attachment_storage
        with self.app.app_context():
            storage = private_attachment_storage()
            client = boto3.client('s3', endpoint_url=ENDPOINT, region_name='auto',
                                  aws_access_key_id=PRIVATE_ACCESS, aws_secret_access_key=PRIVATE_SECRET)
            stubber = Stubber(client)
            stubber.add_response('put_object', {'ETag': '"fixture-upload"'}, {
                'Bucket': PRIVATE_BUCKET, 'Key': KEY, 'Body': DATA,
                'ContentType': 'application/pdf', 'CacheControl': 'private, no-store',
                'Metadata': {'sha256': SHA256},
            })
            try:
                with stubber, patch.object(PrivateAttachmentStorage, '_create_client', return_value=client):
                    reference = storage.upload_file_with_key(io.BytesIO(DATA), KEY, 'application/pdf')
                    stubber.assert_no_pending_responses()
                self.assertEqual(reference, REFERENCE)
                self.assertNotIn('Disposable', reference)
            finally:
                client.close()

    def test_private_upload_enforces_bounded_read_before_put(self):
        from services.private_attachment_storage import private_attachment_storage, MAX_PRIVATE_ATTACHMENT_BYTES
        from unittest.mock import Mock
        class Oversize:
            def __init__(self):
                self.requested = []
            def read(self, size):
                self.requested.append(size)
                return b'x' * size
        stream = Oversize()
        with self.app.app_context():
            storage = private_attachment_storage()
            client = Mock()
            with patch.object(storage, '_get_client', return_value=client):
                self.assertIsNone(storage.upload_file_with_key(stream, KEY, 'application/pdf'))
                self.assertIsNone(storage.upload_file_with_key(io.BytesIO(b''), KEY, 'application/pdf'))
            self.assertEqual(stream.requested, [MAX_PRIVATE_ATTACHMENT_BYTES + 1])
            client.put_object.assert_not_called()

    def test_public_catalog_avatar_event_logo_and_audio_keep_general_storage(self):
        from flask import g
        from database import db
        from models import User
        from services import gcs_service
        from services.attachment_delivery import resolve_attachment_delivery_url
        from services.r2_service import r2_service
        from werkzeug.datastructures import FileStorage

        def upload(stream, key, mime):
            self.assertEqual(stream.read(), DATA)
            self.assertTrue(r2_service.is_public_asset_key(key, mime))
            return r2_service.public_url_for_key(key)

        with self.app.test_request_context('/_acceptance/upload'):
            g.current_user = db.session.get(User, self.accounts['acceptance-a']['id'])
            with patch('services.private_attachment_storage.private_attachment_storage',
                       side_effect=AssertionError('public asset cannot use private storage')), \
                 patch.object(r2_service, 'upload_file_with_key', side_effect=upload):
                for kind in ('catalogos', 'catalog_product_images', 'profile_avatars', 'eventos', 'logos', 'audio'):
                    with self.subTest(kind=kind):
                        result = gcs_service.upload_to_gcs(FileStorage(stream=io.BytesIO(DATA),
                            filename='public-fixture.txt', content_type='text/plain'), kind=kind)
                        self.assertIsNotNone(result)
                        self.assertTrue(result['public_url'].startswith(CDN + '/'))
                        delivery = resolve_attachment_delivery_url(result['public_url'], 'text/plain')
                        self.assertEqual(delivery['url'], result['public_url'])
                        self.assertEqual(delivery['storage_access'], 'public')
                        self.assertFalse(delivery['is_private'])

    def test_unknown_legacy_reference_requires_explicit_verified_mapping(self):
        from services.private_attachment_storage import PrivateAttachmentStorage
        client = self.login()
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('unmapped legacy cannot reach storage')):
            for reference in (LEGACY_CDN, LEGACY_EXTERNAL, '/static/uploads/old.pdf'):
                with self.subTest(reference=reference):
                    self.replace_reference(reference)
                    response = client.get(self.attachment_url())
                    self.assertEqual(response.status_code, 503, response.get_json())
                    self.assertEqual(response.get_json()['reason_code'], 'attachment_private_migration_required')
                    self.assert_no_private_delivery(response)
                    self.assertNotIn(reference, response.get_data(as_text=True))

    def test_legacy_mapping_verifies_exact_object_hash_size_mime(self):
        client = self.login()
        self.replace_reference(LEGACY_EXTERNAL)
        self.mapped()
        with self.sdk_head():
            response = client.get(self.attachment_url())
        self.assert_private_redirect(response)
        for metadata in ({'ContentLength': len(DATA) + 1},
                         {'ContentType': 'text/plain'},
                         {'Metadata': {'sha256': 'c' * 64}},
                         {'ETag': '"different-etag"'}):
            with self.subTest(metadata=metadata), self.sdk_head(metadata=metadata):
                response = client.get(self.attachment_url())
            self.assertEqual(response.status_code, 503, response.get_json())
            self.assertEqual(response.get_json()['reason_code'], 'attachment_private_delivery_unavailable')
            self.assert_no_private_delivery(response)

    def test_legacy_mapping_is_exact_and_requires_receipt_without_sdk(self):
        from services.private_attachment_storage import PrivateAttachmentStorage
        client = self.login()
        self.replace_reference(LEGACY_EXTERNAL)
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('untrusted migration cannot call SDK')):
            self.mapped(LEGACY_EXTERNAL + '?different=1')
            response = client.get(self.attachment_url())
            self.assertEqual(response.get_json()['reason_code'], 'attachment_private_migration_required')
            self.mapped(tenant_slug='acceptance-b')
            response = client.get(self.attachment_url())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()['reason_code'], 'attachment_private_reference_invalid')
            for changes in ({'verification_receipt_id': ''}, {'sha256': 'not-a-hash'},
                            {'byte_size': True}, {'mime_type': None},
                            {'tenant_slug': ''}, {'private_object_etag': None}):
                with self.subTest(changes=changes):
                    self.mapped(**changes)
                    response = client.get(self.attachment_url())
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.get_json()['reason_code'], 'attachment_private_migration_required')
                    self.assert_no_private_delivery(response)

    def test_auth_json_never_initializes_attachment_sdk(self):
        from services.private_attachment_storage import PrivateAttachmentStorage
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('unrelated identity JSON must not touch storage')) as factory:
            client = self.login()
            for path in ('/auth/me', '/api/me', '/auth/session/bootstrap'):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, response.get_json())
            factory.assert_not_called()

    def test_anonymous_nested_redaction_hides_raw_known_legacy_and_external_attachment_refs(self):
        from services.private_attachment_storage import PrivateAttachmentStorage
        from services.attachment_delivery import serialize_attachment_for_delivery
        self.app.config['PRIVATE_ATTACHMENT_TEST_PAYLOAD'] = {
            'unrelated_url': 'https://example.invalid/help',
            'nested': [{'attachmentInfo': {'filename': 'fixture.pdf', 'mimeType': 'application/pdf',
                'url': REFERENCE, 'storage_url': REFERENCE,
                'meta': {'storage_url': LEGACY_EXTERNAL, 'source_url': LEGACY_EXTERNAL}}},
                {'archivo': {'filename': 'legacy.pdf', 'url': LEGACY_CDN}},
                {'attachmentInfo': {'filename': 'external.pdf', 'url': LEGACY_EXTERNAL}}],
        }
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('anonymous metadata cannot initialize SDK')):
            response = self.app.test_client().get('/_acceptance/private-attachments/raw')
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['unrelated_url'], 'https://example.invalid/help')
            self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
            for value in (REFERENCE, LEGACY_CDN, LEGACY_EXTERNAL, '"storage_url"'):
                self.assertNotIn(value, response.get_data(as_text=True))
            with self.app.test_request_context('/_acceptance/anonymous'):
                result = serialize_attachment_for_delivery({'url': REFERENCE, 'filename': FILENAME},
                    meta={'original': REFERENCE, 'nested': [{'storage_url': REFERENCE}]})
                self.assertIsNone(result['url'])
                self.assertFalse(result['is_private'])
                self.assertEqual(result['storage_access'], 'unavailable')
                self.assertNotIn(REFERENCE, json.dumps(result))

    def test_authenticated_raw_response_cannot_use_redactor_as_signing_oracle(self):
        from services.private_attachment_storage import PrivateAttachmentStorage
        client = self.login()
        self.mapped()
        self.replace_reference(LEGACY_EXTERNAL)
        public_url = CDN + '/general/catalog_product_images/acceptance-a/public-fixture.jpg'
        self.app.config['PRIVATE_ATTACHMENT_TEST_PAYLOAD'] = {
            'attachments': [{'id': self.attachment_id, 'filename': FILENAME,
                'url': value, 'storage_url': value, 'is_private': True}
                for value in (REFERENCE, LEGACY_EXTERNAL, LEGACY_CDN)],
            'public_product': {'url': public_url, 'filename': 'product.jpg'},
        }
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('response redactor cannot mint signed URL')) as factory:
            response = client.get('/_acceptance/private-attachments/authenticated-raw')
        self.assertEqual(response.status_code, 200, response.get_json())
        for item in response.get_json()['attachments']:
            self.assertIsNone(item['url'])
            self.assertFalse(item['is_private'])
            self.assertEqual(item['storage_access'], 'unavailable')
            self.assertNotIn('storage_url', item)
        self.assertEqual(response.get_json()['public_product']['url'], public_url)
        self.assert_no_private_delivery(response)
        factory.assert_not_called()

    def test_response_deduplicates_private_head_but_does_not_reuse_across_requests(self):
        client = self.login()
        with self.sdk_head(count=2):
            first = client.get('/_acceptance/private-attachments/authenticated')
            second = client.get('/_acceptance/private-attachments/authenticated')
        for response in (first, second):
            self.assertEqual(response.status_code, 200, response.get_json())
            payload = response.get_json()['attachmentInfo']
            self.assertIsNotNone(payload['url'], payload)
            self.assertEqual(payload['url'], payload['download_url'])
            self.assertEqual(payload['url'], payload['meta']['original'])
            self.assertIn('X-Amz-Expires=120', payload['url'])
            self.assertNotIn(REFERENCE, response.get_data(as_text=True))

    def test_session_list_resolves_exact_resources_and_preserves_unavailable_legacy_row(self):
        from database import db
        from models import ArchivoAdjunto
        from services.private_attachment_storage import PrivateAttachmentStorage
        client = self.login()
        with self.app.app_context():
            row = ArchivoAdjunto(user_id=self.accounts['second']['id'], session_id=SESSION_ID,
                filename='same-tenant-wrong-owner.pdf', nombre_original='Wrong owner fixture.pdf',
                mime='application/pdf', tamano=len(DATA), tipo='chat',
                url=REFERENCE.replace('b' * 32, 'c' * 32))
            db.session.add(row)
            db.session.commit()
        path = '/archivos/sesion/' + SESSION_ID
        with self.sdk_head():
            response = client.get(path)
        self.assertEqual(response.status_code, 200, response.get_json())
        items = response.get_json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['nombre'], 'Disposable scan.pdf')
        self.assertEqual(items[0]['storage_access'], 'signed')
        self.assertTrue(items[0]['is_private'])
        self.assertEqual(parse_qs(urlsplit(items[0]['url']).query)['X-Amz-Expires'], ['120'])
        self.assertNotIn('Wrong owner fixture', response.get_data(as_text=True))
        self.assertNotIn(REFERENCE, response.get_data(as_text=True))
        self.replace_reference(LEGACY_EXTERNAL)
        with patch.object(PrivateAttachmentStorage, '_create_client',
                          side_effect=AssertionError('session list cannot fetch unmapped legacy')) as factory:
            response = client.get(path)
        self.assertEqual(response.status_code, 200, response.get_json())
        items = response.get_json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['nombre'], 'Disposable scan.pdf')
        self.assertIsNone(items[0]['url'])
        self.assertFalse(items[0]['is_private'])
        self.assertEqual(items[0]['reason_code'], 'attachment_private_migration_required')
        self.assertTrue(items[0]['availability_message'])
        self.assertNotIn(LEGACY_EXTERNAL, response.get_data(as_text=True))
        self.assertNotIn('Wrong owner fixture', response.get_data(as_text=True))
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
