"""Real public blueprint over a disposable Flask app; not full app/session acceptance."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_config_under_test', ROOT / 'routes' / 'config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeRecoveryConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, FRONTEND_VERSION='web-test', BACKEND_VERSION='api-test',
            BACKEND_URL='https://api.example.test', PANEL_URL='https://panel.example.test',
            SECRET_KEY='must-not-leak', DATABASE_URL='private-database', TWILIO_AUTH_TOKEN='private-provider')
        self.app.register_blueprint(module.config_bp)
        self.client = self.app.test_client()

    def test_serves_exact_backend_file_as_public_json(self):
        response = self.client.get('/api/config/runtime-recovery')
        expected = json.loads((ROOT / 'config' / 'runtime_recovery_ui.json').read_text(encoding='utf-8'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        self.assertEqual(response.mimetype, 'application/json')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertLess(len(response.data), 8192)

    def test_publishes_all_states_and_button_labels(self):
        data = self.client.get('/api/config/runtime-recovery').get_json()
        self.assertEqual(data['contract_version'], 'chatboc.runtime_recovery_ui.v1')
        self.assertEqual(data['scope'], 'platform')
        self.assertEqual(set(data['states']), {'offline', 'checking', 'waiting', 'verified', 'unavailable', 'mismatch'})
        for name in ['region_label', 'check_label', 'dismiss_label']:
            self.assertTrue(data[name].strip())
        for state in data['states'].values():
            self.assertTrue(state['title'].strip())
            self.assertTrue(state['detail'].strip())

    def test_never_serializes_application_secrets(self):
        body = self.client.get('/api/config/runtime-recovery').get_data(as_text=True)
        for secret in ['must-not-leak', 'private-database', 'private-provider', 'SECRET_KEY', 'DATABASE_URL']:
            self.assertNotIn(secret, body)

    def test_does_not_reflect_requested_tenant_or_actor(self):
        first = self.client.get('/api/config/runtime-recovery').get_json()
        second = self.client.get('/api/config/runtime-recovery?tenant_slug=private-client',
            headers={'X-Tenant-Slug': 'private-client', 'Authorization': 'Bearer private-actor'}).get_json()
        self.assertEqual(first, second)
        self.assertNotIn('private-client', json.dumps(second))

    def test_rejects_business_write_methods(self):
        for method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            with self.subTest(method=method):
                self.assertEqual(self.client.open('/api/config/runtime-recovery', method=method).status_code, 405)

    def test_preserves_existing_version_contract(self):
        response = self.client.get('/api/version')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'frontend': 'web-test', 'backend': 'api-test'})

    def test_preserves_existing_public_configuration(self):
        response = self.client.get('/api/config')
        self.assertEqual(response.get_json(), {'frontendVersion': 'web-test', 'backendVersion': 'api-test',
            'backendUrl': 'https://api.example.test', 'panelUrl': 'https://panel.example.test'})

    def test_missing_file_fails_without_leaking_diagnostics(self):
        with patch.object(module, '_load_runtime_recovery_ui', side_effect=OSError('private-path')):
            response = self.client.get('/api/config/runtime-recovery')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {'error': 'runtime_recovery_config_unavailable'})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_invalid_file_fails_without_leaking_diagnostics(self):
        with patch.object(module, '_load_runtime_recovery_ui', side_effect=ValueError('private-detail')):
            response = self.client.get('/api/config/runtime-recovery')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-detail', response.get_data(as_text=True))

    def test_response_is_not_a_readiness_or_delivery_receipt(self):
        data = self.client.get('/api/config/runtime-recovery').get_json()
        self.assertEqual(set(data), {'contract_version', 'scope', 'region_label', 'check_label', 'dismiss_label', 'states'})
        self.assertNotIn('Set-Cookie', self.client.get('/api/config/runtime-recovery').headers)


if __name__ == '__main__':
    unittest.main()
