"""Disposable HTTP and real-SDK speech checks; no customer/provider calls."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__': prepare_process()

from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from tests import institutional_assistant_http as knowledge_http
from tests.test_institutional_assistant_content import sample
from services.institutional_assistant_content import ContentError

MP3 = b'ID3\x04\x00\x00\x00\x00\x00\x00local-synthetic-audio'


class InstitutionalAudioHTTPTests(unittest.TestCase):
    setUpClass = classmethod(knowledge_http.KnowledgeHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(knowledge_http.KnowledgeHTTPTests.tearDownClass.__func__)
    login = knowledge_http.KnowledgeHTTPTests.login
    url = knowledge_http.KnowledgeHTTPTests.url
    put = knowledge_http.KnowledgeHTTPTests.put

    def setUp(self):
        knowledge_http.KnowledgeHTTPTests.setUp(self)
        self.environ = patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-audio-key'})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        from extensions import limiter
        with self.app.app_context(): limiter.reset()

    def publish(self, client=None, bundle=None):
        client = client or self.login()
        bundle = bundle or sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        private = self.put(client, 'import', None, bundle)
        self.assertEqual(private.status_code, 200, private.get_json())
        public = self.put(client, 'publish', private.get_json()['revision'])
        self.assertEqual(public.status_code, 200, public.get_json())
        return public.get_json()

    def post(self, state, ids=None, **kwargs):
        return self.app.test_client().post(self.url(True) + '/audio',
            json={'revision': state['revision'], 'node_ids': ids or ['requirements']}, **kwargs)

    def test_binary_response_reads_canonical_content_only_and_keeps_source_private(self):
        from models import TenantConfig, AuditEvent, db
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['sources']['a'].update(document_visibility='private',
            origin_url='https://drive.google.com/file/d/PRIVATE/view',
            official_url='https://example.org/PRIVATE.pdf')
        bundle['node_evidence']['start'][0]['quote'] = 'PRIVATE source excerpt.'
        state = self.publish(bundle=bundle)
        with self.app.app_context():
            before = deepcopy(TenantConfig.query.filter_by(tenant_id=self.accounts['acceptance-a']['tenant_id'],
                key='institutional_assistant').one().json_value)
            audit_before = AuditEvent.query.count()
        with (patch('services.openai_tts_bridge.synthesize_mp3_bytes', return_value=MP3) as synthesize,
                patch.object(db.session, 'commit', side_effect=AssertionError('audio must not commit'))):
            response = self.post(state, ['start', 'requirements'])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.data, MP3)
        self.assertEqual(response.content_type, 'audio/mpeg')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(response.headers['X-Chatboc-Knowledge-Revision'], state['revision'])
        self.assertEqual(response.headers['X-Chatboc-Tenant-ID'], str(state['tenant']['id']))
        self.assertEqual(response.headers['Content-Length'], str(len(MP3)))
        text = synthesize.call_args.args[0]
        self.assertIn('Elegí una consulta.', text)
        self.assertIn('Opción 1: Requisitos.', text)
        self.assertIn('Opción 9: Volver.', text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('Documento de prueba', text)
        self.assertNotIn('https://', text)
        with self.app.app_context():
            self.assertEqual(TenantConfig.query.filter_by(tenant_id=state['tenant']['id'],
                key='institutional_assistant').one().json_value, before)
            self.assertEqual(AuditEvent.query.count(), audit_before)

    def test_capability_is_optional_published_only_and_no_provider_calls_on_workspace(self):
        client = self.login()
        private = self.put(client, 'import', None,
            sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')).get_json()
        self.assertNotIn('audio_reading', private)
        state = self.put(client, 'publish', private['revision']).get_json()
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes', side_effect=AssertionError('not manual')):
            capability = self.app.test_client().get(self.url(True)).get_json()['audio_reading']
        self.assertEqual(set(capability), {'contract_version', 'listen', 'pause', 'resume', 'stop',
            'loading', 'error', 'disclosure'})
        self.assertEqual(capability['contract_version'], 'chatboc.institutional_audio.v1')
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            self.assertNotIn('audio_reading', self.app.test_client().get(self.url(True)).get_json())
            self.assertEqual(self.post(state).status_code, 503)
        from models import User, db
        from services.institutional_assistant import maybe_handle_institutional_question
        with self.app.app_context():
            owner = db.session.get(User, self.accounts['acceptance-a']['id'])
            result = maybe_handle_institutional_question('menu', owner)
            self.assertEqual(result['knowledge_audio_reading'], capability)

    def test_post_contract_rejects_user_text_duplicates_queries_and_get_without_provider(self):
        state = self.publish()
        path = self.url(True) + '/audio'
        client = self.app.test_client()
        invalid = [None, {}, {'revision': state['revision']},
            {'revision': state['revision'], 'node_ids': ['start'], 'text': 'caller text'},
            {'revision': state['revision'], 'node_ids': []},
            {'revision': state['revision'], 'node_ids': ['start', 'start']},
            {'revision': state['revision'], 'node_ids': ['start'] * 4},
            {'revision': state['revision'], 'node_ids': [None]},
            {'revision': state['revision'], 'node_ids': ['../secret']},
            {'revision': 'A' * 64, 'node_ids': ['start']}]
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes') as synthesize:
            for command in invalid:
                with self.subTest(command=command):
                    response = client.post(path, json=command)
                    self.assertEqual(response.status_code, 400, response.get_json())
            duplicate = '{"revision":"' + state['revision'] + '","node_ids":["start"],"node_ids":["requirements"]}'
            self.assertEqual(client.post(path, data=duplicate, content_type='application/json').status_code, 400)
            self.assertEqual(client.post(path, data='x' * 2049, content_type='application/json').status_code, 400)
            self.assertEqual(client.post(path, data={'revision': state['revision'], 'node_ids': 'start'}).status_code, 413)
            command = {'revision': state['revision'], 'node_ids': ['start']}
            self.assertEqual(client.post(path + '?text=anything', json=command).status_code, 400)
            self.assertEqual(client.post(path, json=command, headers={'X-Tenant': 'acceptance-b'}).status_code, 400)
            self.assertEqual(client.get(path).status_code, 405)
            synthesize.assert_not_called()

    def test_private_stale_unknown_foreign_and_disabled_tenant_do_not_consume_audio(self):
        from models import TenantProfile, db
        client = self.login()
        private = self.put(client, 'import', None,
            sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')).get_json()
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes') as synthesize:
            self.assertEqual(self.post(private).status_code, 404)
            public = self.put(client, 'publish', private['revision']).get_json()
            self.assertEqual(self.post(private).status_code, 412)
            self.assertEqual(self.post(public, ['missing']).status_code, 400)
            foreign = self.app.test_client().post(self.url(True).replace('acceptance-a', 'acceptance-b') + '/audio',
                json={'revision': public['revision'], 'node_ids': ['requirements']})
            self.assertEqual(foreign.status_code, 404)
            with self.app.app_context():
                tenant = db.session.get(TenantProfile, public['tenant']['id'])
                tenant.is_active = False; db.session.commit()
            self.assertEqual(self.post(public).status_code, 404)
            synthesize.assert_not_called()

    def test_retiring_or_replacing_version_during_generation_releases_no_audio(self):
        from models import TenantConfig, db
        from services.institutional_assistant_content import digest
        for action in ('retire', 'replace', 'disable'):
            with self.subTest(action=action):
                knowledge_http.KnowledgeHTTPTests.setUp(self)
                from extensions import limiter
                with self.app.app_context(): limiter.reset()
                state = self.publish()
                def change_while_generating(*args, **kwargs):
                    if action == 'disable':
                        from models import TenantProfile
                        db.session.get(TenantProfile, state['tenant']['id']).is_active = False
                    else:
                        row = TenantConfig.query.filter_by(tenant_id=state['tenant']['id'], key='institutional_assistant').one()
                        changed = deepcopy(row.json_value)
                        changed['generation'] += 1
                        if action == 'retire': changed['visibility'] = 'private'
                        changed['revision'] = digest({key: changed[key] for key in ('bundle_hash', 'generation', 'visibility')})
                        row.json_value = changed
                    db.session.commit()
                    return MP3
                with patch('services.openai_tts_bridge.synthesize_mp3_bytes', side_effect=change_while_generating):
                    response = self.post(state)
                self.assertEqual(response.status_code, 412 if action == 'replace' else 404)
                self.assertNotIn(MP3, response.data)
                self.assertNotIn('X-Chatboc-Knowledge-Revision', response.headers)

    def test_input_limit_and_missing_shared_storage_fail_before_provider(self):
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['nodes']['requirements']['text'] = 'a' * 4000
        state = self.publish(bundle=bundle)
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes') as synthesize:
            self.assertEqual(self.post(state).status_code, 413)
            synthesize.assert_not_called()
        with patch.dict(os.environ, {'VERCEL': '1'}):
            self.assertNotIn('audio_reading', self.app.test_client().get(self.url(True)).get_json())
            with patch('services.openai_tts_bridge.synthesize_mp3_bytes') as synthesize:
                response = self.post(state, ['start'])
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()['reason_code'], 'knowledge_audio_rate_limit_unavailable')
                synthesize.assert_not_called()

    def test_rate_limits_and_backend_outage_fail_closed_before_provider(self):
        from extensions import limiter
        state = self.publish()
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes', return_value=MP3) as synthesize:
            for _ in range(6): self.assertEqual(self.post(state).status_code, 200)
            limited = self.post(state)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.get_json()['reason_code'], 'knowledge_audio_rate_limited')
            self.assertEqual(synthesize.call_count, 6)
        with self.app.app_context():
            limiter.reset()
            strategy = limiter.limiter
        with (patch.object(strategy, 'hit', side_effect=RuntimeError('SECRET outage details')),
                patch('services.openai_tts_bridge.synthesize_mp3_bytes') as synthesize):
            response = self.post(state)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SECRET', response.get_data(as_text=True))
        synthesize.assert_not_called()

    def test_cross_origin_response_exposes_scope_without_credentials_or_prefetch(self):
        state = self.publish()
        path = self.url(True) + '/audio'
        client = self.app.test_client()
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes', return_value=MP3) as synthesize:
            response = client.options(path, headers={'Origin': 'https://institution.example.invalid',
                'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
            self.assertIn(response.status_code, (200, 204))
            synthesize.assert_not_called()
            response = self.post(state, headers={'Origin': 'https://institution.example.invalid'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], 'https://institution.example.invalid')
        self.assertNotEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')
        exposed = response.headers['Access-Control-Expose-Headers'].lower()
        self.assertIn('x-chatboc-knowledge-revision', exposed)
        self.assertIn('x-chatboc-tenant-id', exposed)

    def test_rotating_ip_addresses_cannot_remove_the_tenant_cost_ceiling(self):
        state = self.publish()
        with patch('services.openai_tts_bridge.synthesize_mp3_bytes', return_value=MP3) as synthesize:
            for index in range(60):
                response = self.post(state, environ_overrides={'REMOTE_ADDR': f'192.0.2.{index + 1}'})
                self.assertEqual(response.status_code, 200, response.get_json())
            response = self.post(state, environ_overrides={'REMOTE_ADDR': '192.0.2.100'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()['reason_code'], 'knowledge_audio_rate_limited')
        self.assertEqual(synthesize.call_count, 60)


class OpenAIBoundedSpeechTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-audio-key',
            'OPENAI_TTS_MODEL': 'gpt-4o-mini-tts', 'OPENAI_BASE_URL': 'https://evil.invalid',
            'HTTPS_PROXY': 'https://evil.invalid'})
        self.environ.start(); self.addCleanup(self.environ.stop)

    def synthesize(self, handler, **kwargs):
        import httpx
        from services.openai_tts_bridge import synthesize_mp3_bytes
        real_client = httpx.Client
        captured = []
        class StubClient(real_client):
            def __init__(self, **client_kwargs):
                captured.append(client_kwargs)
                super().__init__(**client_kwargs, transport=httpx.MockTransport(handler))
        with patch('services.openai_tts_bridge.httpx.Client', StubClient):
            audio = synthesize_mp3_bytes('Texto canónico.', max_bytes=kwargs.get('max_bytes', 1024), timeout_seconds=20)
        return audio, captured

    def test_real_sdk_uses_official_endpoint_mp3_no_retry_and_returns_bytes_without_disk(self):
        import httpx
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), 'https://api.openai.com/v1/audio/speech')
            payload = json.loads(request.content)
            self.assertEqual(payload['input'], 'Texto canónico.')
            self.assertEqual(payload['response_format'], 'mp3')
            self.assertEqual(payload['speed'], 0.9)
            return httpx.Response(200, content=MP3, headers={'Content-Type': 'audio/mpeg'})
        with patch('services.openai_tts_bridge.os.makedirs', side_effect=AssertionError('no disk')):
            audio, config = self.synthesize(handler)
        self.assertEqual(audio, MP3)
        self.assertEqual(len(calls), 1)
        self.assertFalse(config[0]['trust_env'])
        self.assertFalse(config[0]['follow_redirects'])
        self.assertIsNone(config[0]['proxy'])
        self.assertEqual(config[0]['timeout'].read, 5)

    def test_provider_timeout_rate_error_invalid_mime_empty_and_oversize_are_fixed_errors(self):
        import httpx
        cases = [
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout('SECRET', request=request)),
            lambda request: httpx.Response(429, json={'error': {'message': 'SECRET'}}),
            lambda request: httpx.Response(200, content=b'<html>SECRET</html>', headers={'Content-Type': 'text/html'}),
            lambda request: httpx.Response(200, content=b'<html>SECRET</html>', headers={'Content-Type': 'audio/mpeg'}),
            lambda request: httpx.Response(200, content=b'', headers={'Content-Type': 'audio/mpeg'}),
            lambda request: httpx.Response(200, content=MP3 * 100, headers={'Content-Type': 'audio/mpeg'}),
        ]
        for handler in cases:
            with self.subTest(handler=handler), self.assertRaises(ContentError) as raised:
                self.synthesize(handler)
            self.assertEqual(raised.exception.code, 'knowledge_audio_failed')
            self.assertEqual(raised.exception.status, 502)
        calls = []
        def unavailable(request):
            calls.append(request)
            return httpx.Response(503, json={'error': {'message': 'SECRET'}})
        with self.assertRaises(ContentError): self.synthesize(unavailable)
        self.assertEqual(len(calls), 1, 'costly retries must stay disabled')

    def test_total_deadline_rejects_late_audio_and_unknown_model_before_network(self):
        import httpx
        handler = lambda request: httpx.Response(200, content=MP3, headers={'Content-Type': 'audio/mpeg'})
        with patch('services.openai_tts_bridge.time.monotonic', side_effect=[0, 21]), self.assertRaises(ContentError):
            self.synthesize(handler)
        with (patch.dict(os.environ, {'OPENAI_TTS_MODEL': 'arbitrary-unknown-model'}),
                self.assertRaises(ContentError) as raised):
            self.synthesize(lambda request: self.fail('unsupported model must not call provider'))
        self.assertEqual(raised.exception.code, 'knowledge_audio_unavailable')


if __name__ == '__main__': unittest.main(verbosity=2)
