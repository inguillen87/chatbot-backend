import json
import math
import unittest
from unittest.mock import Mock, patch
from scripts.verify_migration_runtime import verify, validate_target, fetch_json

REV = 'a' * 40
BASE = 'https://api-preview.chatboc.ar'
VERSION = {'backend': REV}
READY = {'contract_version': 'runtime.readiness.v1', 'ready': True, 'status': 'ready',
    'components': {'database': {'status': 'ok', 'required': True},
                   'redis': {'status': 'ok', 'required': True}}}
COLD = {'contract_version': 'chatboc.bootstrap.v1', 'retryable': True,
        'reason_code': 'application_initializing'}


class MigrationRuntimeProbeTests(unittest.TestCase):
    def run_probe(self, replies, **kwargs):
        fetch, sleep = Mock(side_effect=replies), Mock()
        result = verify(BASE, REV, fetch=fetch, sleep=sleep, **kwargs)
        return result, fetch, sleep

    def test_requires_revision_dependency_health_and_same_revision_again(self):
        result, fetch, _ = self.run_probe([(200, VERSION), (200, READY), (200, VERSION)])
        self.assertTrue(result['runtime_ready'])
        self.assertTrue(result['first_attempt_ready'])
        self.assertFalse(result['cutover_authorized'])
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
            [BASE+'/api/version', BASE+'/health/ready', BASE+'/api/version'])

    def test_bootstrap_recovery_is_not_reported_as_first_attempt_success(self):
        result, fetch, sleep = self.run_probe([(503,COLD),(200,VERSION),(200,READY),(200,VERSION)])
        self.assertTrue(result['runtime_ready'])
        self.assertFalse(result['first_attempt_ready'])
        sleep.assert_called_once_with(2)
        self.assertEqual(fetch.call_count, 4)

    def test_retries_stop_at_limit_and_never_retry_other_failures(self):
        result, fetch, sleep = self.run_probe([(503,COLD)]*3, attempts=3)
        self.assertFalse(result['runtime_ready'])
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        for status, payload in [(503,{}),(500,COLD),(401,{}),(302,{}),(200,{}),
                (503,dict(COLD,retryable='true')),(503,dict(COLD,reason_code='application_initialization_failed'))]:
            with self.subTest(status=status,payload=payload):
                result, fetch, sleep = self.run_probe([(status,payload)])
                self.assertFalse(result['runtime_ready'])
                self.assertEqual(fetch.call_count,1)
                sleep.assert_not_called()

    def test_deployment_changed_between_probes_fails_closed(self):
        result, _, _ = self.run_probe([(200,VERSION),(200,READY),(200,{'backend':'b'*40})])
        self.assertFalse(result['runtime_ready'])
        self.assertEqual(result['samples'][-1]['reason'], 'revision_mismatch')

    def test_simple_health_or_optional_database_is_not_readiness(self):
        invalid = [{'status':'ok'},dict(READY,components={}),dict(READY,ready='true'),
            dict(READY,components=dict(READY['components'],database={'status':'ok','required':False})),
            dict(READY,components=dict(READY['components'],redis={'status':'error','required':True}))]
        for payload in invalid:
            with self.subTest(payload=payload):
                result, _, _ = self.run_probe([(200,VERSION),(200,payload)])
                self.assertFalse(result['runtime_ready'])

    def test_payloads_and_exception_text_are_not_copied_to_evidence(self):
        secret='PRIVATE-CUSTOMER-CONTENT'
        result, _, _ = self.run_probe([(500,{'error':secret})])
        self.assertNotIn(secret,json.dumps(result))
        fetch=Mock(side_effect=ValueError(secret))
        result=verify(BASE,REV,fetch=fetch)
        self.assertNotIn(secret,json.dumps(result))

    def test_denies_production_credentials_queries_ports_and_lookalike_hosts(self):
        for base in ['http://api-preview.chatboc.ar','https://api.chatboc.ar',
            BASE+'?token=test',BASE+'#x',BASE+':443',BASE+'/api/version',
            'https://user:pass@api-preview.chatboc.ar',BASE+'.evil.test',
            'https://other.vercel.app','https://localhost','https://127.0.0.1']:
            with self.subTest(base=base), self.assertRaises(ValueError):
                validate_target(base,REV)
        self.assertEqual(validate_target(BASE+'/',REV), BASE)

    def test_invalid_budgets_and_symbolic_revision_never_send_network_requests(self):
        for kwargs in [{'attempts':0},{'attempts':7},{'attempts':True},
                {'timeout':math.nan},{'timeout':math.inf},{'timeout':0},{'timeout':16}]:
            fetch=Mock()
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                verify(BASE, REV, fetch=fetch, **kwargs)
            fetch.assert_not_called()
        with self.assertRaises(ValueError):
            validate_target(BASE,'main')

    @patch('scripts.verify_migration_runtime.shutil.which', return_value='curl')
    @patch('scripts.verify_migration_runtime.subprocess.run')
    def test_curl_uses_safe_flags_and_sanitizes_html_and_oversized_bodies(self, run, _):
        run.return_value=Mock(returncode=0,stdout=b'{"backend":"'+REV.encode()+b'"}\n200\napplication/json')
        self.assertEqual(fetch_json(BASE+'/api/version',5),(200,VERSION))
        command=run.call_args.args[0]
        self.assertEqual(command[:2],['curl','--disable'])
        self.assertNotIn('--location',command)
        self.assertNotIn('--insecure',command)
        self.assertIn('--max-filesize',command)
        run.return_value=Mock(returncode=0,stdout=b'<html>private</html>\n200\ntext/html')
        self.assertEqual(fetch_json(BASE+'/api/version',5),(200,{}))
        run.return_value=Mock(returncode=0,stdout=b'x'*66001)
        self.assertEqual(fetch_json(BASE+'/api/version',5),(0,{}))

if __name__ == '__main__':
    unittest.main()
