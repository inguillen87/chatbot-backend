"""Read-only release guard tests; no live endpoints, customer data or deployment."""
import json
import unittest
from unittest.mock import Mock, patch
from scripts.verify_paired_preview import verify_pair, validate_frontend, frontend_matches, fetch_document

FRONT = 'https://chatboc-frontend-candidate-marcelos-projects-c26aa499.vercel.app'
BACK = 'https://chatboc-backend-candidate-marcelos-projects-c26aa499.vercel.app'
FSHA, BSHA = 'a' * 40, 'b' * 40

def html(sha=FSHA):
    return f'<!doctype html><head><meta name="chatboc-build-revision" content="{sha}"></head>'

def ready():
    return {'contract_version':'runtime.readiness.v1', 'ready':True, 'status':'ready',
            'components':{'database':{'status':'ok','required':True}, 'redis':{'status':'ok','required':True}}}

def response(status, body, mime='application/json'):
    return status, mime, json.dumps(body) if not isinstance(body, str) else body

def normal(url, timeout):
    if '/perfil?' in url: return response(200,html(),'text/html')
    if url.endswith('/health/ready'): return response(200,ready())
    if url.endswith('/api/me'): return response(401,{'error':'Unauthorized'})
    return response(200,{'backend':BSHA,'frontend':'web'})

def starting():
    return response(503,{'contract_version':'chatboc.bootstrap.v1',
        'reason_code':'application_initializing','retryable':True})

class PairedPreviewTests(unittest.TestCase):
    def verify(self, **kwargs):
        return verify_pair(FRONT,BACK,FSHA,BSHA,**kwargs)

    def test_healthy_pair_checks_frontend_proxy_not_only_the_backend(self):
        fetch=Mock(side_effect=normal)
        result=self.verify(fetch=fetch)
        self.assertTrue(result['version_pair_verified'])
        self.assertTrue(result['first_attempt_ready'])
        self.assertEqual(fetch.call_count,7)
        urls=[call.args[0] for call in fetch.call_args_list]
        self.assertEqual(urls.count(FRONT+'/api/version'),2)
        self.assertFalse(result['promotion_authorized'])
        self.assertFalse(result['authenticated_acceptance'])
        self.assertFalse(result['writes_performed'])

    def test_new_frontend_pointing_at_old_backend_is_rejected(self):
        fetch=Mock(side_effect=lambda url,t: response(200,{'backend':'c'*40})
            if url==FRONT+'/api/version' else normal(url,t))
        result=self.verify(fetch=fetch)
        self.assertFalse(result['version_pair_verified'])
        self.assertEqual(result['failure'],'proxied_backend:revision_mismatch')
        self.assertEqual(fetch.call_count,4)

    def test_alias_drift_after_first_success_is_detected(self):
        count=0
        def fetch(url,t):
            nonlocal count
            if url==FRONT+'/api/version':
                count+=1
                if count==2:return response(200,{'backend':'c'*40})
            return normal(url,t)
        result=self.verify(fetch=fetch)
        self.assertEqual(result['failure'],'proxied_backend_recheck:revision_mismatch')

    def test_frontend_drift_after_first_success_is_detected(self):
        count=0
        def fetch(url,t):
            nonlocal count
            if '/perfil?' in url:
                count+=1
                if count==2:return response(200,html('c'*40),'text/html')
            return normal(url,t)
        self.assertEqual(self.verify(fetch=fetch)['failure'],'frontend_recheck:frontend_revision_mismatch')

    def test_explicit_bootstrap_retry_is_not_first_attempt_readiness(self):
        called=False
        def fetch(url,t):
            nonlocal called
            if url==BACK+'/api/version' and not called:
                called=True;return starting()
            return normal(url,t)
        sleep=Mock()
        result=self.verify(fetch=fetch,sleep=sleep)
        self.assertTrue(result['version_pair_verified'])
        self.assertFalse(result['first_attempt_ready'])
        sleep.assert_called_once_with(2)

    def test_generic_503_is_not_retried(self):
        fetch=Mock(side_effect=lambda url,t: response(503,{'error':'private database detail'})
            if url==BACK+'/api/version' else normal(url,t))
        sleep=Mock(); result=self.verify(fetch=fetch,sleep=sleep)
        self.assertFalse(result['version_pair_verified'])
        sleep.assert_not_called()
        self.assertNotIn('private database detail',json.dumps(result))

    def test_terminal_bootstrap_failure_is_not_retried(self):
        fetch=Mock(side_effect=lambda url,t: response(503,{'contract_version':'chatboc.bootstrap.v1',
            'reason_code':'application_initialization_failed','retryable':False}) if url==BACK+'/api/version' else normal(url,t))
        sleep=Mock();result=self.verify(fetch=fetch,sleep=sleep)
        self.assertFalse(result['version_pair_verified']);sleep.assert_not_called()

    def test_retries_are_bounded(self):
        fetch=Mock(side_effect=lambda url,t: starting() if url==BACK+'/api/version' else normal(url,t))
        result=self.verify(fetch=fetch,sleep=Mock(),attempts=2)
        self.assertFalse(result['version_pair_verified'])
        self.assertEqual(fetch.call_count,3)

    def test_components_must_be_required_and_ready(self):
        for field in ('database','redis'):
            with self.subTest(field=field):
                payload=ready();payload['components'][field]['required']=False
                fetch=lambda url,t: response(200,payload) if url.endswith('/health/ready') else normal(url,t)
                self.assertEqual(self.verify(fetch=fetch)['failure'],'readiness:required_components_not_ready')

    def test_anonymous_profile_must_not_be_readable(self):
        fetch=lambda url,t: response(200,{'user':'private'}) if url.endswith('/api/me') else normal(url,t)
        result=self.verify(fetch=fetch)
        self.assertEqual(result['failure'],'access:anonymous_profile_not_rejected')
        self.assertNotIn('private',json.dumps(result))

    def test_production_or_credentialed_origins_fail_before_fetch(self):
        for origin in ('https://chatboc.ar','https://evil.vercel.app','http://127.0.0.1',
            FRONT+'?token=private',FRONT+'/other',FRONT+'#hash',FRONT.replace('https://','https://u:p@'),FRONT+':443'):
            with self.subTest(origin=origin):
                fetch=Mock()
                with self.assertRaises(ValueError): verify_pair(origin,BACK,FSHA,BSHA,fetch=fetch)
                fetch.assert_not_called()
        with self.assertRaises(ValueError): verify_pair(FRONT,'https://api.chatboc.ar',FSHA,BSHA,fetch=Mock())

    def test_frontend_requires_one_real_metadata_tag_not_a_sha_elsewhere(self):
        self.assertTrue(frontend_matches(html(),FSHA))
        for body in (FSHA, '<script>'+html()+'</script>', html()+html(),
            '<meta name="chatboc-build-revision" content="'+FSHA+'" content="bad">',html('x'*40)):
            with self.subTest(body=body):self.assertFalse(frontend_matches(body,FSHA))

    def test_wrong_mime_and_redirects_fail_closed(self):
        for status,mime in ((200,'text/plain'),(302,'text/html'),(200,'application/json')):
            with self.subTest(status=status,mime=mime):
                result=self.verify(fetch=lambda url,t:(status,mime,html()))
                self.assertFalse(result['version_pair_verified'])

    def test_invalid_parameters_never_trigger_network(self):
        for patch in ({'attempts':True},{'attempts':0},{'timeout':float('nan')},{'budget':999}, {'timeout':True}):
            with self.subTest(patch=patch):
                fetch=Mock()
                with self.assertRaises(ValueError):self.verify(fetch=fetch,**patch)
                fetch.assert_not_called()
        for sha in ('main','a'*39,'A'*40,''):
            with self.subTest(sha=sha),self.assertRaises(ValueError):validate_frontend(FRONT,sha)

    def test_deadline_is_not_success_even_after_a_late_response(self):
        clock=Mock(side_effect=[0,0,16,16])
        result=self.verify(fetch=normal,clock=clock,budget=15)
        self.assertEqual(result['failure'],'verification_deadline_exceeded')

    def test_response_size_and_invalid_transport_are_rejected(self):
        for payload in ((200,'text/html','x'*65537),(True,'text/html',html()),(200,None,html())):
            with self.subTest(payload_type=str(type(payload))):
                self.assertFalse(self.verify(fetch=lambda url,t:payload)['version_pair_verified'])

    def test_transport_exceptions_do_not_expose_their_details(self):
        result=self.verify(fetch=Mock(side_effect=RuntimeError('secret connection string')))
        self.assertFalse(result['version_pair_verified'])
        self.assertNotIn('secret',json.dumps(result))

    def test_curl_does_not_use_redirects_credentials_or_curlrc(self):
        fake=Mock(returncode=0,stdout=b'{}\n200\napplication/json')
        with patch('scripts.verify_paired_preview.shutil.which',return_value='curl'),\
             patch('scripts.verify_paired_preview.subprocess.run',return_value=fake) as run:
            self.assertEqual(fetch_document(BACK+'/api/version',3),(200,'application/json','{}'))
        command=run.call_args.args[0]
        self.assertEqual(command[1],'--disable')
        self.assertEqual(command[command.index('--max-redirs')+1],'0')
        self.assertNotIn('--location',command)
        self.assertNotIn('--cookie',command)
        self.assertNotIn('--user',command)

if __name__=='__main__':unittest.main(verbosity=2)
