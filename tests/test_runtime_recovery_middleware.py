"""Exercise the actual registered tenant hook and public blueprint.

Model/resolver import dependencies are isolated, not a full create_app/DB test.
The hook itself is loaded unchanged and the tenant resolver is a strict spy.
"""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, g

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


models = ModuleType('models')
models.TenantProfile = type('TenantProfile', (), {})
utils = ModuleType('utils')
utils.__path__ = []
tenant = ModuleType('utils.tenant')
tenant.get_current_tenant_profile = MagicMock()
tenant.get_current_tenant_slug = MagicMock()
tenant.require_tenant = MagicMock()
sqlalchemy = ModuleType('sqlalchemy')
sqlalchemy.func = MagicMock()
with patch.dict(sys.modules, {'models': models, 'utils': utils, 'utils.tenant': tenant, 'sqlalchemy': sqlalchemy}):
    middleware = load('runtime_tenant_hook_under_test', 'middleware/tenant_context.py')
config = load('runtime_public_config_under_test', 'routes/config.py')


class RuntimeRecoveryMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, BACKEND_URL='https://api.example.test',
            PANEL_URL='https://panel.example.test', FRONTEND_VERSION='web', BACKEND_VERSION='api')

        @self.app.before_request
        def earlier_context():
            g.tenant_profile = object()
            g.tenant_profile_slug = 'earlier-context'
            g.current_tenant = 'earlier-context'
            g.current_tenant_slug = 'earlier-context'

        # This is the real registration function imported by middleware/__init__.py
        # and called as tenant_middleware(app) by app.py.
        middleware.tenant_middleware(self.app)
        self.app.register_blueprint(config.config_bp)
        self.app.add_url_rule('/api/config/runtime-recovery/private', 'private_example', lambda: ('', 403))
        self.client = self.app.test_client()
        self.resolver_patch = patch.object(middleware, '_resolve_tenant_profile',
            side_effect=AssertionError('tenant lookup must not run for platform recovery copy'))
        self.resolver = self.resolver_patch.start()
        self.addCleanup(self.resolver_patch.stop)
        self.expected = json.loads((ROOT / 'config/runtime_recovery_ui.json').read_text(encoding='utf-8'))

    def test_anonymous_get_bypasses_tenant_lookup(self):
        response = self.client.get('/api/config/runtime-recovery')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), self.expected)
        self.resolver.assert_not_called()

    def test_tenant_hints_do_not_trigger_database_resolution_or_change_copy(self):
        response = self.client.get('/api/config/runtime-recovery?tenant_slug=private-client&tenant_id=42',
            headers={'X-Tenant-Slug': 'private-client', 'X-Widget-Token': 'private-widget',
                'X-Forwarded-Host': 'private-client.example.test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), self.expected)
        self.resolver.assert_not_called()

    def test_platform_response_clears_context_from_earlier_hooks(self):
        with self.client as client:
            response = client.get('/api/config/runtime-recovery')
            self.assertEqual(response.status_code, 200)
            for name in ['tenant_profile', 'tenant_profile_slug', 'current_tenant', 'current_tenant_slug']:
                self.assertIsNone(getattr(g, name))
        self.resolver.assert_not_called()

    def test_head_is_also_independent_of_tenant_lookup(self):
        response = self.client.head('/api/config/runtime-recovery')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'')
        self.resolver.assert_not_called()

    def test_config_failure_does_not_fall_back_to_tenant_resolution(self):
        with patch.object(config, '_load_runtime_recovery_ui', side_effect=OSError('private-path')):
            response = self.client.get('/api/config/runtime-recovery')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.resolver.assert_not_called()

    def test_nearby_private_path_keeps_tenant_hook_and_denial(self):
        self.resolver.side_effect = None
        self.resolver.return_value = None
        response = self.client.get('/api/config/runtime-recovery/private')
        self.assertEqual(response.status_code, 403)
        self.resolver.assert_called_once()

    def test_existing_configuration_endpoint_keeps_its_original_hook(self):
        self.resolver.side_effect = None
        self.resolver.return_value = None
        response = self.client.get('/api/config')
        self.assertEqual(response.status_code, 200)
        self.resolver.assert_called_once()

    def test_unmatched_similar_path_is_not_exempted_by_prefix(self):
        self.resolver.side_effect = None
        self.resolver.return_value = None
        response = self.client.get('/api/config/runtime-recovery-extra')
        self.assertEqual(response.status_code, 404)
        self.resolver.assert_called_once()


if __name__ == '__main__':
    unittest.main()
